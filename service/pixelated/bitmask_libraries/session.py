#
# Copyright (c) 2014 ThoughtWorks, Inc.
#
# Pixelated is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pixelated is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Pixelated. If not, see <http://www.gnu.org/licenses/>.
import errno
import traceback
import sys
import os
import requests
import logging

from twisted.internet import reactor, defer
from pixelated.bitmask_libraries.certs import LeapCertificate
from pixelated.adapter.mailstore import LeapMailStore
from leap.mail.incoming.service import IncomingMail
from leap.mail.imap.account import IMAPAccount
from leap.auth import SRPAuth
from .nicknym import NickNym
from .smtp import LeapSMTPConfig
from .soledad import SoledadFactory

from leap.common.events import (
    register, unregister,
    catalog as events
)


log = logging.getLogger(__name__)


SESSIONS = {}   # TODO replace with redis or memCached in prod


class LeapSession(object):

    def __init__(self, provider, user_auth, mail_store, soledad, nicknym, smtp_config):
        self.smtp_config = smtp_config
        self.config = provider.config
        self.provider = provider
        self.user_auth = user_auth
        self.mail_store = mail_store
        self.soledad = soledad
        self.nicknym = nicknym
        self.fresh_account = False
        self.incoming_mail_fetcher = None
        self.account = None
        register(events.KEYMANAGER_FINISHED_KEY_GENERATION, self._set_fresh_account, uid=self.account_email())

    @defer.inlineCallbacks
    def initial_sync(self):
        yield self.sync()
        yield self.after_first_sync()
        defer.returnValue(self)

    @defer.inlineCallbacks
    def after_first_sync(self):
        yield self.nicknym.generate_openpgp_key()
        self.account = self._create_account(self.account_email, self.soledad)
        self.incoming_mail_fetcher = yield self._create_incoming_mail_fetcher(
            self.nicknym,
            self.soledad,
            self.account,
            self.account_email())
        reactor.callFromThread(self.incoming_mail_fetcher.startService)

    def _create_account(self, user_mail, soledad):
        account = IMAPAccount(user_mail, soledad)
        return account

    def _set_fresh_account(self, event, email_address):
        log.debug('Key for email %s has been generated' % email_address)
        if email_address == self.account_email():
            self.fresh_account = True

    def account_email(self):
        name = self.user_auth.username
        return self.provider.address_for(name)

    def close(self):
        self.stop_background_jobs()
        unregister(events.KEYMANAGER_FINISHED_KEY_GENERATION, uid=self.account_email())
        self.soledad.close()
        self.remove_from_cache()

    def remove_from_cache(self):
        key = SessionCache.session_key(self.provider, self.user_auth.username)
        SessionCache.remove_session(key)

    @defer.inlineCallbacks
    def _create_incoming_mail_fetcher(self, nicknym, soledad, account, user_mail):
        inbox = yield account.callWhenReady(lambda _: account.getMailbox('INBOX'))
        defer.returnValue(IncomingMail(nicknym.keymanager,
                          soledad,
                          inbox.collection,
                          user_mail))

    def stop_background_jobs(self):
        if self.incoming_mail_fetcher:
            reactor.callFromThread(self.incoming_mail_fetcher.stopService)
            self.incoming_mail_fetcher = None

    def sync(self):
        try:
            return self.soledad.sync()
        except:
            traceback.print_exc(file=sys.stderr)
            raise


class SmtpCertDownloader(object):

    def __init__(self, provider, auth):
        self._provider = provider
        self._auth = auth

    def download(self):
        cert_url = '%s/%s/cert' % (self._provider.api_uri, self._provider.api_version)
        cookies = {"_session_id": self._auth.session_id}
        headers = {}
        headers["Authorization"] = 'Token token="{0}"'.format(self._auth.token)
        response = requests.get(
            cert_url,
            verify=LeapCertificate(self._provider).provider_api_cert,
            cookies=cookies,
            timeout=self._provider.config.timeout_in_s,
            headers=headers)
        response.raise_for_status()

        client_cert = response.content

        return client_cert

    def download_to(self, target_file):
        client_cert = self.download()

        with open(target_file, 'w') as f:
            f.write(client_cert)


class LeapSessionFactory(object):
    def __init__(self, provider):
        self._provider = provider
        self._config = provider.config

    def create(self, username, password, auth=None):
        key = SessionCache.session_key(self._provider, username)
        session = SessionCache.lookup_session(key)
        if not session:
            session = self._create_new_session(username, password, auth)
            SessionCache.remember_session(key, session)

        return session

    def _auth_leap(self, username, password):
        srp_auth = SRPAuth(self._provider.api_uri, self._provider.local_ca_crt)
        return srp_auth.authenticate(username, password)

    def _create_new_session(self, username, password, auth=None):
        self._create_dir(self._provider.config.leap_home)
        self._provider.download_certificate()

        auth = auth or self._auth_leap(username, password)
        account_email = self._provider.address_for(username)

        self._create_database_dir(auth.uuid)

        soledad = SoledadFactory.create(auth.token,
                                        auth.uuid,
                                        password,
                                        self._secrets_path(auth.uuid),
                                        self._local_db_path(auth.uuid),
                                        self._provider.discover_soledad_server(auth.uuid),
                                        LeapCertificate(self._provider).provider_api_cert)

        mail_store = LeapMailStore(soledad)
        nicknym = self._create_nicknym(account_email, auth.token, auth.uuid, soledad)

        self._download_smtp_cert(auth)
        smtp_host, smtp_port = self._provider.smtp_info()
        smtp_config = LeapSMTPConfig(account_email, self._smtp_client_cert_path(), smtp_host, smtp_port)

        return LeapSession(self._provider, auth, mail_store, soledad, nicknym, smtp_config)

    def _download_smtp_cert(self, auth):
        cert_path = self._smtp_client_cert_path()

        if not os.path.exists(os.path.dirname(cert_path)):
            os.makedirs(os.path.dirname(cert_path))

        SmtpCertDownloader(self._provider, auth).download_to(cert_path)

    def _smtp_client_cert_path(self):
        return os.path.join(
            self._config.leap_home,
            "providers",
            self._provider.domain,
            "keys", "client", "smtp.pem")

    def _create_dir(self, path):
        try:
            os.makedirs(path)
        except OSError as exc:
            if exc.errno == errno.EEXIST and os.path.isdir(path):
                pass
            else:
                raise

    def _create_nicknym(self, email_address, token, uuid, soledad):
        return NickNym(self._provider, self._config, soledad, email_address, token, uuid)

    def _soledad_path(self, user_uuid):
        return os.path.join(self._config.leap_home, user_uuid, 'soledad')

    def _secrets_path(self, user_uuid):
        return os.path.join(self._soledad_path(user_uuid), 'secrets')

    def _local_db_path(self, user_uuid):
        return os.path.join(self._soledad_path(user_uuid), 'soledad.db')

    def _create_database_dir(self, user_uuid):
        try:
            os.makedirs(self._soledad_path(user_uuid))
        except OSError as exc:
            if exc.errno == errno.EEXIST and os.path.isdir(self._soledad_path(user_uuid)):
                pass
            else:
                raise


class SessionCache(object):     # should be replaced with redis or memcached in prod

    @staticmethod
    def lookup_session(key):
        global SESSIONS
        return SESSIONS.get(key, None)

    @staticmethod
    def remember_session(key, session):
        global SESSIONS
        SESSIONS[key] = session

    @staticmethod
    def remove_session(key):
        global SESSIONS
        if key in SESSIONS:
            del SESSIONS[key]

    @staticmethod
    def session_key(provider, username):
        return hash((provider, username))
