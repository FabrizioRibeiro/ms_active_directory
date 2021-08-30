import binascii

from ldap3 import Connection
from ldap3.core.exceptions import LDAPInvalidDnError
from ldap3.utils.dn import parse_dn
from typing import List


from ms_active_directory import logging_utils
from ms_active_directory.environment.ldap.ldap_constants import (
    ADObject,
    AD_USERNAME_RESTRICTED_CHARS,
    SAM_ACCOUNT_NAME_LENGTH,
    LEGACY_SAM_ACCOUNT_NAME_LENGTH_LIMIT,
)
from ms_active_directory.exceptions import (
    InvalidDomainParameterException,
    InvalidLdapParameterException,
    ObjectNotFoundException,
)


logger = logging_utils.get_logger()


def is_dn(anything: str):
    """ Determine if a specified string is a distinguished name. """
    try:
        # our sessions all set check_names to the default value of True, so dns will be escaped
        # in search bases and such. therefore we escape them here.
        # if this is a check for filtering, then we'll be getting the unescaped value to figure out
        # how to best escape it, so definitely escape it to avoid errors from special characters
        parse_dn(anything, escape=True)
        return True
    except LDAPInvalidDnError:
        return False


def construct_default_hostnames_for_computer(computer_name: str, domain_dns_name: str):
    """ Construct the default hostnames for a computer in AD. The short hostname is the computer name capitalized,
    and the fqdn is lowercase of the computer name dot the domain.
    """
    return [computer_name.upper(), computer_name.lower() + '.' + domain_dns_name.lower()]


def construct_object_distinguished_name(object_name: str, object_location: str, domain: str):
    """
    Constructs the distinguished name of a computer, group, or user given the name, join location, and domain.
    """
    computer_part = 'CN=' + object_name
    domain_part = construct_ldap_base_dn_from_domain(domain)
    return ','.join([computer_part, object_location, domain_part])


def construct_domain_from_ldap_base_dn(domain: str):
    """
    Given a base DN, constructs the DNS name of the AD domain.
    """
    dn_split = parse_dn(domain)
    # parse dn takes "cn=demo,ou=Computers,dc=example,DC=com" and turns it into
    # [('cn', 'demo', ','), ('ou', 'Computers', ','), ('dc', 'example', ','), ('DC', 'com', '')]
    domain_pieces = [piece[1] for piece in dn_split if piece[0].upper() == 'DC']
    return '.'.join(domain_pieces)


def construct_ldap_base_dn_from_domain(domain: str):
    """
    Given a domain, constructs the base dn.
    """
    domain_split = domain.split('.')
    return ','.join(map(lambda x: 'DC=' + x, domain_split))


def construct_service_principal_names(services: List[str], hostnames: List[str]):
    """ Given a list of services and hostnames, construct the kerberos server principle names for them. """
    spns = []
    for serv in services:
        for hostname in hostnames:
            spns.append(serv + '/' + hostname)
    return spns


def escape_generic_filter_value(anything: str):
    """ Escape anything, so that it can be used in ldap queries without confusing the server.
    According to the LDAP spec, there's a set of common characters that need escaping:
    rfc4514 (https://tools.ietf.org/html/rfc4514).

    RFCs that define new LDAP attributes, as well different server types, may require
    additional characters be escaped. Additionally, not all characters need to be escaped.
    For example, many versions of AD do not require commas be escaped, but will be ok if
    they are. Please ensure you know what you're escaping before calling this.
    See escape_dn_for_filter for an example of an alternative escape function needed to
    escape a field with different properties.
    """
    if anything.isalnum():
        return anything

    def escape_char(char):
        """ Escape a single character."""
        if char in "*()\\/\0 \t\r\n+<>,\";":
            # LDAP query language is really forgiving about strange characters.
            # rfc2254 says the only characters to escape are "*{}\\\0". AD adds "/" to the
            # list, and OpenLDAP adds whitespace. Over-escaping is safe, so just do everything
            # every time.
            return "\\%02x" % ord(char)
        else:
            return char
    return "".join(escape_char(x) for x in anything)


def escape_dn_for_filter(anything: str):
    """Escape an LDAP distinguished name so that it can be used in filters without confusing the server.
    Distinguished names already have some special characters escaped or encoded, so we must use this
    function instead of the generic escape function, which would escape the existing escape sequences.

    In a filter, you use the format field=value.
    But distinguished names are in the form CN=x,OU=y,DC=z so those equal signs need to be escaped.
    But then the values x, y, and z can also have equal signs in them, and those will ALREADY be escaped
    differently from the ones following CN, OU, etc.
    That's why DNs need a different escaping in filters than everything else.
    """
    if isinstance(anything, int) or isinstance(anything, float):
        return anything

    if anything.isalnum():
        return anything

    def escape_char(char):
        """ Escape a single character."""
        if char in "()*":
            return "\\%02x" % ord(char)
        else:
            return char
    return "".join(escape_char(x) for x in anything)


def escape_bytestring_for_filter(byte_str: bytes):
    """ Escape any bytestring (e.g. SIDs) for use in an LDAP filter.
    It will be converted to a hex string first and then escaped.
    If it is already a string, it will be escaped as if it were a hex string.
    """
    if isinstance(byte_str, bytes):
        hex_str = binascii.hexlify(byte_str).decode('UTF-8')
    else:
        hex_str = byte_str
    hex_escape_char = '\\'
    # 2 hex characters make up 1 byte, and the LDAP syntax for filtering on a bytestring is to escape
    # each byte with a backslash while representing them as hex.
    # see: http://www.ietf.org/rfc/rfc2254.txt
    return hex_escape_char + hex_escape_char.join(hex_str[i:i+2] for i in range(0, len(hex_str), 2))


def normalize_entities_to_entity_dns(entities: List, lookup_by_name_fn: callable, controls: List):
    """ Given a list of entities that might be AD objects or strings, return a map of LDAP distinguished names
    for the entities.
    """
    # make a map of entity distinguished names to entities passed in. we'll use this when constructing
    # our return dictionary as well
    entity_dns = {}
    for entity in entities:
        if isinstance(entity, str):
            if is_dn(entity):
                # cast to lowercase for case-insensitive checks later
                entity_dn = entity.lower()
            elif lookup_by_name_fn is None:
                raise InvalidLdapParameterException('If any entities are strings and are not distinguished names, '
                                                    'a function must be provided to look up entities by name. '
                                                    'Context: {}'.format(entity))
            else:
                entity_obj = lookup_by_name_fn(entity, controls=controls)
                if entity_obj is None:
                    raise ObjectNotFoundException('No entity could be found with name {}'.format(entity))
                # cast to lowercase for case-insensitive checks later
                entity_dn = entity_obj.distinguished_name.lower()
        elif isinstance(entity, ADObject):
            # cast to lowercase for case-insensitive checks later
            entity_dn = entity.distinguished_name.lower()
        else:
            bad_type = type(entity)
            raise InvalidLdapParameterException('All entities must either be ADObject objects or strings. {} was '
                                                'of type {}'.format(entity, bad_type))
        entity_dns[entity_dn] = entity
    return entity_dns


def normalize_object_location_in_domain(location: str, domain_dns_name: str):
    """ There's two main formats we might see used for an object location - LDAP style and Windows Path style.
    For each style, they can be relative or fully qualified.

    LDAP Style looks like this:
    OU=Location
    or fully qualified:
    OU=Location,DC=example,DC=com

    Windows Path Style looks like this:
    computers/
    or fully qualified:
    example.com/computers

    This function tries to normalize everything to the LDAP style using the relative DN format, since the library
    is primarily LDAP based.
    Currently, windows path style will be rejected. This is because we'd need to make LDAP queries at each level
    to determine the proper naming attribute for the next level (e.g. CN or OU). We can do that, but we'd need a
    connection already established or credentials.
    """
    # TODO: make windows path style names work
    if not is_dn(location):
        raise NotImplementedError('Windows-style paths are not supported')
    return strip_domain_from_object_location(location, domain_dns_name)


def process_ldap3_conn_return_value(ldap_connection: Connection, return_value, paginated_response=False):
    """ Thread-safe ldap3 connections return a tuple containing a boolean about success,
    the result, the response, and the request. Non-thread-safe ldap3 connections just
    leave the other fields and return a boolean when performing search/add/etc. and
    leave it up to the caller to manage thread safety.

    This function processes the return value so that it can be used within this class
    without worrying about the return format.
    """
    # thread-safe strategies return response tuples of (success, result, response, request)
    # but paginated searches in the ldap3 library only return the response no matter what, and
    # the thread-safe unpacking is handled internally during accumulation
    if ldap_connection.strategy.thread_safe and not paginated_response:
        success, result, response, req = return_value
    else:
        success = return_value
        result = ldap_connection.result
        response = ldap_connection.response
        req = ldap_connection.request
    return success, result, response, req


def remove_ad_search_refs(response):
    """ Many LDAP queries in Active Directory will include a number of generic search references
    to say 'maybe go look here for completeness'. This is especially common in setups where
    there's trusted domains or other domains in the same forest.

    But for many domain operations, we don't care about them. For example, when checking for other
    accounts with a sAMAccountName in a domain, we don't care about the possibility of accounts
    existing with that name in another domain in the forest because it's a domain-unique
    attribute.
    So this is a helper function to remove such references.

    :param response: A list of LDAP search responses.
    :return: A filtered list, with search references removed.
    """
    if not response:
        return []
    real_entities = []
    if response:
        real_entities = [entry for entry in response if entry.get('dn')]
    return real_entities


def strip_domain_from_object_location(location: str, domain_dns_name: str):
    """ Our object Location in a domain should be a relative distinguished name (RDN), but if someone specifies the full
    path, let's be forgiving.
    This is a normalizing function to convert to RDNs.
    So if a user specifies "OU=Location,DC=example,DC=com" this function will strip off "DC=example,DC=com"
    and leave the relative distinguished name "OU=Location" which is what we'll actually use.
    """
    if location is None:
        return location

    # cast everything to uppercase in order to avoid worrying about how a customer chose to type their DN.
    # place a comma in front of the domain RDN so that any stripping we do will strip the trailing comma
    domain_rdn_upper = ',' + construct_ldap_base_dn_from_domain(domain_dns_name).upper()
    location = location.upper()
    if location.endswith(domain_rdn_upper):
        trim_length = len(domain_rdn_upper)
        # trim the length of our domain RDN from the end
        location = location[:-trim_length]

    return location


def validate_and_normalize_computer_name(name: str, supports_legacy_behavior: bool):
    """ Computer common names are sAMAccountNames without the $ at the end. So check for allowable
    characters and length limits.
    """
    limit = LEGACY_SAM_ACCOUNT_NAME_LENGTH_LIMIT if supports_legacy_behavior else SAM_ACCOUNT_NAME_LENGTH
    # peel off the ending $ if present
    if name.endswith('$'):
        name = name[:-1]
    if len(name) > limit:
        raise InvalidDomainParameterException('Computer name length must be fewer than {} characters for computers '
                                              'that {} legacy behavior.'.format(limit, 'support' if supports_legacy_behavior else 'do not support'))
    for character in AD_USERNAME_RESTRICTED_CHARS:
        if character in name:
            raise InvalidDomainParameterException('AD computer names may not contain any of the following characters: '
                                                  '{}'.format(', '.join(AD_USERNAME_RESTRICTED_CHARS)))
    return name