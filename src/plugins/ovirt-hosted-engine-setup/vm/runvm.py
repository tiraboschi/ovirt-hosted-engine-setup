#
# ovirt-hosted-engine-setup -- ovirt hosted engine setup
# Copyright (C) 2013-2015 Red Hat, Inc.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#


"""
VM configuration plugin.
"""


import gettext
import socket


from otopi import constants as otopicons
from otopi import plugin
from otopi import util
from vdsm import vdscli


from ovirt_hosted_engine_setup import check_liveliness
from ovirt_hosted_engine_setup import constants as ohostedcons
from ovirt_hosted_engine_setup import mixins


def _(m):
    return gettext.dgettext(message=m, domain='ovirt-hosted-engine-setup')


@util.export
class Plugin(mixins.VmOperations, plugin.PluginBase):
    """
    VM configuration plugin.
    """

    def __init__(self, context):
        super(Plugin, self).__init__(context=context)

    @plugin.event(
        stage=plugin.Stages.STAGE_INIT,
    )
    def _init(self):
        self.environment.setdefault(
            ohostedcons.VMEnv.VM_PASSWD,
            self._generateTempVncPassword()
        )
        self.environment[otopicons.CoreEnv.LOG_FILTER_KEYS].append(
            ohostedcons.VMEnv.VM_PASSWD
        )
        self.environment.setdefault(
            ohostedcons.VMEnv.VM_PASSWD_VALIDITY_SECS,
            ohostedcons.Defaults.DEFAULT_VM_PASSWD_VALIDITY_SECS
        )
        self.environment.setdefault(
            ohostedcons.VMEnv.CONSOLE_TYPE,
            None
        )

    @plugin.event(
        stage=plugin.Stages.STAGE_SETUP,
    )
    def _setup(self):
        self.command.detect('remote-viewer')

    @plugin.event(
        stage=plugin.Stages.STAGE_CUSTOMIZATION,
        condition=lambda self: not self.environment[
            ohostedcons.CoreEnv.IS_ADDITIONAL_HOST
        ],
        after=(
            ohostedcons.Stages.DIALOG_TITLES_S_VM,
        ),
        before=(
            ohostedcons.Stages.DIALOG_TITLES_E_VM,
        ),
    )
    def _customization(self):
        validConsole = False
        interactive = self.environment[
            ohostedcons.VMEnv.CONSOLE_TYPE
        ] is None
        answermap = {
            'vnc': 'vnc',
            'spice': 'qxl'
        }
        while not validConsole:
            if self.environment[
                ohostedcons.VMEnv.CONSOLE_TYPE
            ] is None:
                answer = self.dialog.queryString(
                    name='OVEHOSTED_VM_CONSOLE_TYPE',
                    note=_(
                        'Please specify the console type '
                        'you would like to use to connect '
                        'to the VM (@VALUES@) [@DEFAULT@]: '
                    ),
                    prompt=True,
                    caseSensitive=False,
                    validValues=list(answermap.keys()),
                    default='vnc',
                )

                if answer in answermap.keys():
                    self.environment[
                        ohostedcons.VMEnv.CONSOLE_TYPE
                    ] = answermap[answer]
            if self.environment[
                ohostedcons.VMEnv.CONSOLE_TYPE
            ] in answermap.values():
                validConsole = True
            elif interactive:
                self.logger.error(
                    'Unsuppored console type provided.'
                )
            else:
                raise RuntimeError(
                    _('Unsuppored console type provided.')
                )

    @plugin.event(
        stage=plugin.Stages.STAGE_CLOSEUP,
        name=ohostedcons.Stages.VM_RUNNING,
        priority=plugin.Stages.PRIORITY_LOW,
        condition=lambda self: (
            self.environment[ohostedcons.VMEnv.BOOT] != 'disk' and
            not self.environment[ohostedcons.CoreEnv.IS_ADDITIONAL_HOST]
        ),
    )
    def _boot_from_install_media(self):
        # Need to be done after firewall closeup for allowing the user to
        # connect from remote.
        os_installed = False
        self._create_vm()
        while not os_installed:
            try:
                os_installed = check_liveliness.manualSetupDispatcher(
                    self,
                    check_liveliness.MSD_OS_INSTALLED,
                )
            except socket.error as e:
                self.logger.debug(
                    'Error talking with VDSM (%s), reconnecting.' % str(e),
                    exc_info=True
                )
                cli = vdscli.connect(
                    timeout=ohostedcons.Const.VDSCLI_SSL_TIMEOUT
                )
                self.environment[ohostedcons.VDSMEnv.VDS_CLI] = cli

    @plugin.event(
        stage=plugin.Stages.STAGE_CLOSEUP,
        after=(
            ohostedcons.Stages.OS_INSTALLED,
        ),
        name=ohostedcons.Stages.INSTALLED_VM_RUNNING,
        condition=lambda self: not self.environment[
            ohostedcons.CoreEnv.IS_ADDITIONAL_HOST
        ],
    )
    def _boot_from_hd(self):
        # Temporary attach cloud-init no-cloud iso if we have to
        if (
                self.environment[ohostedcons.VMEnv.BOOT] == 'disk' and
                self.environment[ohostedcons.VMEnv.CDROM]
        ):
            self.environment[
                ohostedcons.VMEnv.SUBST
            ]['@CDROM@'] = self.environment[
                ohostedcons.VMEnv.CDROM
            ]
        created = False
        while not created:
            try:
                self._create_vm()
                created = True
            except socket.error as e:
                self.logger.debug(
                    'Error talking with VDSM (%s), reconnecting.' % str(e),
                    exc_info=True
                )
                cli = vdscli.connect(
                    timeout=ohostedcons.Const.VDSCLI_SSL_TIMEOUT
                )
                self.environment[ohostedcons.VDSMEnv.VDS_CLI] = cli


# vim: expandtab tabstop=4 shiftwidth=4
