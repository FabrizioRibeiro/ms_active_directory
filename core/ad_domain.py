import copy
import socket

import logging_utils

from ldap3 import (
    Connection,
    FIRST,
    KERBEROS,
    NTLM,
    SAFE_RESTARTABLE,
    SASL,
    Server,
    ServerPool,
    SIMPLE,
    Tls,
)
from ssl import (
    OP_NO_SSLv2,
    OP_NO_SSLv3,
    OP_NO_TLSv1,
    OP_NO_TLSv1_1,
)
from typing import List

# local imports come after imports from other libraries
from core.ad_session import ADSession
from environment.discovery_utils import discover_kdc_domain_controllers_in_domain, discover_ldap_domain_controllers_in_domain
from environment.format_utils import format_computer_name_for_authentication
from environment.kerberos_utils.kerberos_constants import DEFAULT_KRB5_KEYTAB_FILE_LOCATION
from environment.security_utils.security_config_constants import ADEncryptionType


logger = logging_utils.get_logger()


def join_ad_domain(domain_dns_name: str, admin_user: str, admin_password: str, authentication_mechanism: str=SIMPLE,
                   computer_name: str=None, computer_location: str=None, computer_password: str=None,
                   computer_encryption_types: List[ADEncryptionType]=None, computer_hostnames: List[str]=None,
                   computer_services: List[str]=None, supports_legacy_behavior: bool=False,
                   computer_key_file_path: str=DEFAULT_KRB5_KEYTAB_FILE_LOCATION, **additional_account_attributes):
    """ A super simple 'join a domain' function that requires minimal input - the domain dns name and admin credentials
    to use in the join process.
    Given those basic inputs, the domain's nearest controllers are automatically discovered and an account is made
    with strong security settings. The account's attributes follow AD naming conventions based on the computer's
    hostname by default.
    """
    domain = ADDomain(domain_dns_name)
    return domain.join(admin_user, admin_password, authentication_mechanism, computer_name=computer_name,
                       computer_location=computer_location, computer_password=computer_password,
                       computer_hostnames=computer_hostnames, computer_services=computer_services,
                       computer_encryption_types=computer_encryption_types, supports_legacy_behavior=supports_legacy_behavior,
                       computer_key_file_path=computer_key_file_path, **additional_account_attributes)


def join_ad_domain_using_session(ad_session: ADSession, computer_name=None, computer_location=None,
                                 computer_password=None, computer_encryption_types=None, computer_hostnames=None,
                                 computer_services=None, supports_legacy_behavior=False,
                                 computer_key_file_path=DEFAULT_KRB5_KEYTAB_FILE_LOCATION,
                                 **additional_account_attributes):
    """ A fairly simple 'join a domain' function that requires minimal input - an AD session and admin credentials
    to use in the join process.
    Given those basic inputs, the domain's nearest controllers are automatically discovered and an account is made
    with strong security settings. The account's attributes follow AD naming conventions based on the computer's
    hostname by default.
    By providing an AD session, one can build a connection to the domain however they so choose and then use it to
    join this computer, so you don't even need to necessarily use user credentials.
    """
    # for joining a domain, default to using the local machine's hostname as a computer name
    if computer_name is None:
        computer_name = socket.gethostname()
        logger.info('Using computer hostname as computer name to join domain: {}'.format(computer_name))
    logger.info('Attempting to join computer to domain %s with name %s', ad_session.get_domain_dns_name(),
                computer_name)
    computer = ad_session.create_computer(computer_name, computer_location=computer_location,
                                          computer_password=computer_password,
                                          encryption_types=computer_encryption_types, hostnames=computer_hostnames,
                                          services=computer_services, supports_legacy_behavior=supports_legacy_behavior,
                                          **additional_account_attributes)
    if computer_key_file_path is not None:
        computer.write_full_keytab_file_for_computer(computer_key_file_path)
    logger.info('Successfully joined computer to domain %s with name %s', ad_session.get_domain_dns_name(),
                computer_name)
    return computer


class ADDomain:

    def __init__(self, domain: str,
                 ldap_servers_or_uris: List = None,
                 kerberos_uris: List = None,
                 encrypt_connections: bool = True,
                 ca_certificates_file_path: str = None,
                 discover_ldap_servers: bool = True,
                 discover_kerberos_servers: bool = True):
        self.domain = domain.lower()  # cast to lowercase
        self.encrypt_connections = encrypt_connections
        self.ca_certificates_file_path = ca_certificates_file_path
        self.ldap_servers = []
        self.ldap_uris = []
        self.kerberos_uris = []
        # discover ldap servers and kerberos servers if we weren't provided any and weren't told not to
        if not ldap_servers_or_uris and discover_ldap_servers:
            ldap_servers_or_uris = discover_ldap_domain_controllers_in_domain(self.domain, secure=self.encrypt_connections)
        # discover kerberos servers if we weren't provided any and weren't told not to
        if not kerberos_uris and discover_kerberos_servers:
            kerberos_uris = discover_kdc_domain_controllers_in_domain(self.domain)

        # handle the fact that user-provided ldap servers could be servers or strings
        if ldap_servers_or_uris:
            self.set_ldap_servers_or_uris(ldap_servers_or_uris)

        if kerberos_uris:
            self.set_kerberos_uris(kerberos_uris)

    def get_domain_dns_name(self):
        return self.domain

    def get_ldap_servers(self):
        return copy.deepcopy(self.ldap_servers)

    def get_ldap_uris(self):
        return copy.deepcopy(self.ldap_uris)

    def get_kerberos_uris(self):
        return copy.deepcopy(self.kerberos_uris)

    def set_ldap_servers_or_uris(self, ldap_servers_or_uris: List):
        """ Set our list of LDAP servers or LDAP URIs. The list provided can be a list of
        Server objects, URIs, or a mixture.
        """
        ldap_uris = []
        ldap_server_objs = []
        # users can specify Server objects if they want a custom Tls setting for
        # each one, but if they provide strings then we just make our own for each
        for serv in ldap_servers_or_uris:
            if isinstance(serv, str):
                # pass through whatever CA certs file we got, and disable all TLS below
                # 1.2 by default
                tls_setting = None
                if self.encrypt_connections:
                    tls_setting = Tls(ca_certs_file=self.ca_certificates_file_path,
                                      ssl_options=[OP_NO_SSLv2, OP_NO_SSLv3, OP_NO_TLSv1,
                                                   OP_NO_TLSv1_1])
                ldap_server_objs.append(Server(serv, tls=tls_setting))
                ldap_uris.append(serv)
            elif isinstance(serv, Server):
                # extract the uri for list of the server uris we're using
                ldap_server_objs.append(serv)
                ldap_uris.append(serv.name)
            else:
                raise Exception('Invalid type for element of ldap server list, {}; '
                                'elements must be strings or Server objects'.format(type(serv)))
        self.ldap_servers = ldap_server_objs
        self.ldap_uris = ldap_uris

    def set_kerberos_uris(self, kerberos_uris: List):
        """ Sets our kerberos server uris """
        for serv in kerberos_uris:
            if not isinstance(serv, str):
                raise Exception('Invalid type for element of kerberos server list, {}; '
                                'elements must be strings'.format(type(serv)))
        self.kerberos_uris = kerberos_uris

    def refresh_ldap_server_discovery(self):
        """ Re-discover LDAP servers in DNS for the domain and redo the sorting by RTT.
        This can update our list of LDAP servers for future connections, allowing faster servers to be
        moved up in priority, unavailable servers to be removed from the list, and previously unavailable
        servers to be added.
        """
        ldap_uris = discover_ldap_domain_controllers_in_domain(self.domain, secure=self.encrypt_connections)
        self.set_ldap_servers_or_uris(ldap_uris)

    def refresh_kerberos_server_discovery(self):
        """ Re-discover Kerberos servers in DNS for the domain and redo the sorting by RTT.
        This can update our list of KDCs for future use by callers, allowing faster servers to be
        moved up in priority, unavailable servers to be removed from the list, and previously unavailable
        servers to be added.
        """
        kerberos_uris = discover_kdc_domain_controllers_in_domain(self.domain)
        self.set_kerberos_uris(kerberos_uris)

    def _create_session(self, user, password, authentication_mechanism, **kwargs):
        # our servers were either user specified (in which case it's a list of ordered preferences) or
        # were discovered automatically (in which case they're ordered by RTT), so use the FIRST strategy
        # to either contact the first preferred server or the fastest/closest server
        server_pool = ServerPool(servers=self.ldap_servers, pool_strategy=FIRST)
        # if no client strategy is specified, default to restartable. AD tends to close idle connections;
        # also if a user specified the LDAP servers, they may have used a hostname that has many servers
        # behind it (like just the domain name), which can cause connections to break if they're using TLS
        # an experience a dns failover on a synchronous connection. Restartable connections avoid these issues.
        # Use safe restartable in case the caller uses this in a multi-threaded application.
        if not kwargs.get('client_strategy'):
            conn = Connection(server_pool, user=user, password=password, authentication=authentication_mechanism,
                              client_strategy=SAFE_RESTARTABLE, **kwargs)
        else:
            conn = Connection(server_pool, user=user, password=password, authentication=authentication_mechanism,
                              **kwargs)

        conn.open()
        logger.debug('Opened connection to AD domain %s: %s', self.domain, conn)
        if self.encrypt_connections:
            # if we're using LDAPS, don't StartTLS
            if not conn.server.ssl:
                tls_started = conn.start_tls()
                if not tls_started:
                    raise Exception('Unable to StartTLS on connection to domain. Please check the server(s) to ensure '
                                    'that they have properly configured certificates.')
            logger.debug('Successfully secured connection to AD domain %s', self.domain)
        bound = conn.bind()
        if not bound:
            raise Exception('Failed to bind connection to {} - please check the credentials and authentication '
                            'mechanism in use.'.format(conn.server.name))
        logger.debug('Successfully bound connection to AD domain %s to establish session', self.domain)
        session = ADSession(conn, self)
        return session

    def create_session_for_user(self, user: str=None, password: str=None, authentication_mechanism: str=None,
                                **kwargs):
        """ Create a session with AD domain authenticated as the specified user. """
        logger.info('Establishing session with AD domain %s using LDAP authentication mechanism %s and user %s',
                    self.domain, authentication_mechanism, user)
        return self._create_session(user, password, authentication_mechanism, **kwargs)

    def create_session_for_computer(self, computer_name: str, computer_password: str=None, check_name_format: bool=True,
                                    authentication_mechanism: str=KERBEROS, **kwargs):
        """ Create a session with AD domain authenticated as the specified computer. """
        # reject simple binds because computers can't use them for authentication
        if authentication_mechanism == SIMPLE:
            raise Exception('Computers must use a form of SASL or NTLM for authenticating LDAP communication with '
                            'and AD domain.')
        logger.info('Establishing session with AD domain %s using LDAP authentication mechanism %s and computer %s',
                    self.domain, authentication_mechanism, computer_name)
        # when using EXTERNAL authentication (certificate-based) there might be some weird names, so let power users
        # skip our helpful name formatting/validation logic in case it causes issues
        formatted_name = computer_name
        if check_name_format:
            formatted_name = format_computer_name_for_authentication(computer_name, self.domain,
                                                                     authentication_mechanism)
        # ntlm isn't real SASL, but Kerberos, EXTERNAL, etc. are
        if authentication_mechanism != NTLM:
            kwargs['sasl_mechanism'] = authentication_mechanism
            authentication_mechanism = SASL
        return self._create_session(formatted_name, computer_password, authentication_mechanism, **kwargs)

    def join(self, admin_username: str, admin_password: str, authentication_mechanism: str=SIMPLE,
             computer_name: str=None, computer_location: str=None, computer_password: str=None,
             computer_encryption_types: List[ADEncryptionType]=None, computer_hostnames: List[str]=None,
             computer_services: List[str]=None, supports_legacy_behavior: bool=False,
             computer_key_file_path: str=DEFAULT_KRB5_KEYTAB_FILE_LOCATION,
             **additional_account_attributes):
        """ A super simple 'join the domain' function that requires minimal input - just admin user credentials
        to use in the join process.
        Given those basic inputs, the domain's settings are used to establish a connection, and an account is made
        with strong security settings. The account's attributes follow AD naming conventions based on the computer's
        hostname by default.
        """
        ad_session = self.create_session_for_user(admin_username, admin_password, authentication_mechanism)
        return join_ad_domain_using_session(ad_session, computer_name=computer_name, computer_location=computer_location,
                                            computer_password=computer_password, computer_hostnames=computer_hostnames,
                                            computer_services=computer_services,
                                            computer_encryption_types=computer_encryption_types,
                                            supports_legacy_behavior=supports_legacy_behavior,
                                            computer_key_file_path=computer_key_file_path,
                                            **additional_account_attributes)

