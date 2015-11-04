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
from StringIO import StringIO
from email.utils import parseaddr
from leap.mail.outgoing.service import OutgoingMail

from twisted.internet.defer import Deferred, fail
from twisted.mail.smtp import SMTPSenderFactory
from twisted.internet import reactor, defer
from pixelated.support.functional import flatten
from twisted.mail.smtp import User


class SMTPDownException(Exception):
    def __init__(self):
        Exception.__init__(self, "Couldn't send mail now, try again later.")


NOT_NEEDED = None


class MailSender(object):

    def __init__(self, smtp_config, keymanager):
        self._smtp_config = smtp_config
        self._keymanager = keymanager

    def sendmail(self, mail):
        recipients = flatten([mail.to, mail.cc, mail.bcc])
        outgoing_mail = self._create_outgoing_mail()
        deferreds = []

        for recipient in recipients:
            smtp_recipient = self._create_twisted_smtp_recipient(recipient)
            deferreds.append(outgoing_mail.send_message(mail.to_smtp_format(), smtp_recipient))

        return defer.gatherResults(deferreds)

    def _create_outgoing_mail(self):
        return OutgoingMail(str(self._smtp_config.account_email),
                            self._keymanager,
                            self._smtp_config.cert_path,
                            self._smtp_config.cert_path,
                            str(self._smtp_config.remote_smtp_host),
                            int(self._smtp_config.remote_smtp_port))

    def _create_twisted_smtp_recipient(self, recipient):
        return User(str(recipient), NOT_NEEDED, NOT_NEEDED, NOT_NEEDED)


class LocalSmtpMailSender(object):

    def __init__(self, account_email_address, smtp):
        self.smtp = smtp
        self.account_email_address = account_email_address

    def sendmail(self, mail):
        if self.smtp.ensure_running():
            recipients = flatten([mail.to, mail.cc, mail.bcc])
            result_deferred = Deferred()
            sender_factory = SMTPSenderFactory(
                fromEmail=self.account_email_address,
                toEmail=set([parseaddr(recipient)[1] for recipient in recipients]),
                file=StringIO(mail.to_smtp_format()),
                deferred=result_deferred)

            reactor.connectTCP('localhost', self.smtp.local_smtp_port_number,
                               sender_factory)

            return result_deferred
        return fail(SMTPDownException())
