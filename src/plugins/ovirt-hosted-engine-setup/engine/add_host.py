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
Host adder plugin.
"""

import gettext
import os
import selinux
import socket
import tempfile
import time


import ovirtsdk.api
import ovirtsdk.infrastructure.errors
import ovirtsdk.xml


from otopi import constants as otopicons
from otopi import filetransaction
from otopi import plugin
from otopi import transaction
from otopi import util


from vdsm import netinfo


from ovirt_hosted_engine_setup import check_liveliness
from ovirt_hosted_engine_setup import constants as ohostedcons
from ovirt_hosted_engine_setup import vds_info
from ovirt_hosted_engine_setup import pkissh


def _(m):
    return gettext.dgettext(message=m, domain='ovirt-hosted-engine-setup')


@util.export
class Plugin(plugin.PluginBase):
    """
    Host adder plugin.
    """

    VDSM_RETRIES = 600
    VDSM_DELAY = 1

    def __init__(self, context):
        super(Plugin, self).__init__(context=context)
        self._ovirtsdk_api = ovirtsdk.api
        self._ovirtsdk_xml = ovirtsdk.xml
        self._interactive_admin_pwd = True

    def _wait_host_ready(self, engine_api, host):
        self.logger.info(_(
            'Waiting for the host to become operational in the engine. '
            'This may take several minutes...'
        ))

        tries = self.VDSM_RETRIES
        isUp = False
        while not isUp and tries > 0:
            tries -= 1
            try:
                state = engine_api.hosts.get(host).status.state
            except Exception as exc:
                # Sadly all ovirtsdk errors inherit only from Exception
                self.logger.debug(
                    'Error fetching host state: {error}'.format(
                        error=str(exc),
                    )
                )
                state = ''
            self.logger.debug(
                'VDSM host in {state} state'.format(
                    state=state,
                )
            )
            if 'failed' in state:
                self.logger.error(_(
                    'The VDSM host was found in a failed state. '
                    'Please check engine and bootstrap installation logs.'
                ))
                tries = -1  # Error state
            elif state == 'up':
                isUp = True
                self.logger.info(_('The VDSM Host is now operational'))
            else:
                if state == 'non_operational':
                    if not self._retry_non_operational(
                        engine_api,
                        self.environment[ohostedcons.EngineEnv.APP_HOST_NAME],
                    ):
                        # It's up, but non-operational and missing some
                        # required networks. _retry_non_operational
                        # already gave enough info, rest of code can assume
                        # it's up.
                        isUp = True
                if tries % 30 == 0:
                    self.logger.info(_(
                        'Still waiting for VDSM host to become operational...'
                    ))
                time.sleep(self.VDSM_DELAY)
        if not isUp and tries == 0:
            self.logger.error(_(
                'Timed out while waiting for host to start. '
                'Please check the logs.'
            ))
        return isUp

    def _retry_non_operational(self, engine_api, host):
        """Return True if we should continue trying to add the host"""
        ret = True
        try:
            cluster = engine_api.clusters.get(
                self.environment[
                    ohostedcons.EngineEnv.HOST_CLUSTER_NAME
                ]
            )
            h = engine_api.hosts.get(host)
            required_networks = set(
                [
                    rn.get_id()
                    for rn in cluster.networks.list(required=True)
                ]
            )
            configured_networks = set(
                [
                    nic.get_network().get_id()
                    for nic in h.nics.list()
                    if nic.get_network()
                ]
            )
            if (
                len(required_networks) > 1 and
                required_networks > configured_networks
            ):
                tbc = required_networks - configured_networks
                rnet = [
                    engine_api.networks.get(id=rn).get_name() for rn in tbc
                ]
                self.dialog.note(
                    _(
                        '\nThe following required networks\n'
                        '  {rnet}\n'
                        'still need to be configured on {host} '
                        'in order to make it\n'
                        'operational. Please setup them via the engine '
                        'webadmin UI or flag them as not required.\n'
                        'When finished, activate the host in the webadmin. '
                    ).format(
                        rnet=rnet,
                        host=host,
                    )
                )
                ret = (
                    False if not self.environment[
                        ohostedcons.NetworkEnv.PROMPT_REQUIRED_NETWORKS
                    ] else
                    self.dialog.queryString(
                        name='OVEHOSTED_REQUIRED_NETWORKS',
                        note=_(
                            'Retry checking host status or ignore this '
                            'and continue '
                            "(@VALUES@)[@DEFAULT@]? "
                        ),
                        prompt=True,
                        validValues=(_('Retry'), _('Ignore')),
                        caseSensitive=False,
                        default=_('Retry'),
                    ) == _('Retry').lower()
                )
                if not ret:
                    self.logger.warning(
                        _('Not waiting for required networks to be set up')
                    )
                    self.dialog.note(
                        _(
                            'To finish deploying, please:\n'
                            '- set up required networks for this host\n'
                            '- activate it\n'
                            '- restart the hosted-engine high availability '
                            'services by running on this machine:\n'
                            '  # service ovirt-ha-agent restart\n'
                            '  # service ovirt-ha-broker restart\n'
                        )
                    )
            else:
                # No missing required networks, perhaps some other issue?
                self.dialog.note(
                    _(
                        'The host {host} is in non-operational state.\n'
                        'Please try to activate it via the engine '
                        'webadmin UI.\n'
                    ).format(
                        host=host,
                    )
                )
                ret = (
                    False if not self.environment[
                        ohostedcons.EngineEnv.PROMPT_NON_OPERATIONAL
                    ] else
                    self.dialog.queryString(
                        name='OVEHOSTED_NON_OPERATIONAL',
                        note=_(
                            'Retry checking host status or ignore this '
                            'and continue '
                            "(@VALUES@)[@DEFAULT@]? "
                        ),
                        prompt=True,
                        validValues=(_('Retry'), _('Ignore')),
                        caseSensitive=False,
                        default=_('Retry'),
                    ) == _('Retry').lower()
                )
                if not ret:
                    self.logger.warning(
                        _('Host left in non-operational state')
                    )
                    self.dialog.note(
                        _(
                            'To finish deploying, please:\n'
                            '- activate it\n'
                            '- restart the hosted-engine high availability '
                            'services by running on this machine:\n'
                            '  # service ovirt-ha-agent restart\n'
                            '  # service ovirt-ha-broker restart\n'
                        )
                    )

        except Exception as exc:
            # Sadly all ovirtsdk errors inherit only from Exception
            self.logger.debug(
                'Error fetching the network configuration: {error}'.format(
                    error=str(exc),
                )
            )
        return ret

    def _wait_cluster_cpu_ready(self, engine_api, cluster_name):
        tries = self.VDSM_RETRIES
        cpu = None
        while cpu is None and tries > 0:
            tries -= 1
            cluster = engine_api.clusters.get(cluster_name)
            cpu = cluster.get_cpu()
            if cpu is None:
                self.logger.debug(
                    'cluster {cluster} cluster.__dict__ {cdict}'.format(
                        cluster=cluster,
                        cdict=cluster.__dict__,
                    )
                )
                if tries % 30 == 0:
                    self.logger.info(
                        _(
                            "Waiting for cluster '{name}' "
                            "to become operational..."
                        ).format(
                            name=cluster.name,
                        )
                    )
                time.sleep(self.VDSM_DELAY)
        if cpu is None and tries == 0:
            self.logger.error(_(
                'Timed out while waiting for cluster to become ready. '
                'Please check the logs.'
            ))
        return cluster, cpu

    def _getCA(self):
        fqdn = self.environment[
            ohostedcons.NetworkEnv.OVIRT_HOSTED_ENGINE_FQDN
        ]
        fd, cert = tempfile.mkstemp(
            prefix='engine-ca',
            suffix='.crt',
        )
        os.close(fd)
        self.environment[
            ohostedcons.EngineEnv.TEMPORARY_CERT_FILE
        ] = cert
        valid = False
        interactive = True
        if self.environment[
            ohostedcons.EngineEnv.INSECURE_SSL
        ]:
            valid = True
        elif self.environment[
            ohostedcons.EngineEnv.INSECURE_SSL
        ] is False:
            interactive = False
        pkihelper = pkissh.PKIHelper()

        while not valid:
            cafile = ohostedcons.FileLocations.SYS_CUSTOMCA_CERT
            if not os.path.isfile(ohostedcons.FileLocations.SYS_CUSTOMCA_CERT):
                cafile = None
            try:
                content = pkihelper.getPKICert(
                    fqdn,
                    cafile,
                )
            except RuntimeError as ex:
                self.logger.error(
                    _('Error acquiring CA cert').format(
                        message=ex.message,
                    )
                )
            else:
                try:
                    with open(cert, 'w') as fileobj:
                        fileobj.write(content)
                except EnvironmentError as ex:
                        raise RuntimeError(
                            'Unable to write cert file: ' + ex.message
                        )
                if pkihelper.validateCA(fqdn, cert):
                    valid = True
            if not valid:
                if interactive:
                    if cafile:
                        catype = _('custom')
                    else:
                        catype = _('internal')
                    insecure = self.dialog.queryString(
                        name='SSL_VALIDATE_CA',
                        note=_(
                            'The REST API cert couldn\'t be trusted with the '
                            '{catype} CA cert\n'
                            'Would you like to continue in insecure mode '
                            '(not recommended)?\n'
                            'If not, please provide your CA cert at {path} '
                            'before continuing\n'
                            '(@VALUES@)[@DEFAULT@]? '
                        ).format(
                            catype=catype,
                            path=ohostedcons.FileLocations.SYS_CUSTOMCA_CERT,
                        ),
                        prompt=True,
                        validValues=(_('Yes'), _('No')),
                        caseSensitive=False,
                        default=_('No')
                    ) == _('Yes').lower()
                    if insecure:
                        valid = True
                        self.environment[
                            ohostedcons.EngineEnv.INSECURE_SSL
                        ] = True
                        cert = self.environment[
                            ohostedcons.EngineEnv.TEMPORARY_CERT_FILE
                        ]
                        if cert is not None and os.path.exists(cert):
                            os.unlink(cert)
                        self.environment[
                            ohostedcons.EngineEnv.TEMPORARY_CERT_FILE
                        ] = None
                else:
                    raise RuntimeError('Failed trusting the REST API cert')

    def _getSSH(self):
        pkihelper = pkissh.PKIHelper()
        authorized_keys_line = pkihelper.getSSHkey(
            fqdn=self.environment[
                ohostedcons.NetworkEnv.OVIRT_HOSTED_ENGINE_FQDN
            ],
            ca_certs=self.environment[
                ohostedcons.EngineEnv.TEMPORARY_CERT_FILE
            ],
        )

        authorized_keys_file = os.path.join(
            os.path.expanduser('~root'),
            '.ssh',
            'authorized_keys'
        )

        content = pkihelper.mergeAuthKeysFile(
            authorized_keys_file, authorized_keys_line
        )
        with transaction.Transaction() as localtransaction:
            localtransaction.append(
                filetransaction.FileTransaction(
                    name=authorized_keys_file,
                    content=content,
                    mode=0o600,
                    owner='root',
                    enforcePermissions=True,
                    modifiedList=self.environment[
                        otopicons.CoreEnv.MODIFIED_FILES
                    ],
                )
            )

        if self._selinux_enabled:
            path = os.path.join(
                os.path.expanduser('~root'),
                '.ssh'
            )
            try:
                selinux.restorecon(path, recursive=True)
            except OSError as ex:
                self.logger.error(
                    _(
                        'Failed to refresh SELINUX context for {path}: {ex}'
                    ).format(
                        path=path,
                        ex=ex.message,
                    )
                )

    @plugin.event(
        stage=plugin.Stages.STAGE_INIT,
    )
    def _init(self):
        self.environment.setdefault(
            ohostedcons.EngineEnv.ADMIN_PASSWORD,
            None
        )
        self.environment[otopicons.CoreEnv.LOG_FILTER_KEYS].append(
            ohostedcons.EngineEnv.ADMIN_PASSWORD
        )
        self.environment.setdefault(
            ohostedcons.EngineEnv.APP_HOST_NAME,
            None
        )
        self.environment.setdefault(
            ohostedcons.EngineEnv.HOST_CLUSTER_NAME,
            None
        )
        self.environment.setdefault(
            ohostedcons.EngineEnv.TEMPORARY_CERT_FILE,
            None
        )
        self.environment.setdefault(
            ohostedcons.NetworkEnv.PROMPT_REQUIRED_NETWORKS,
            True
        )
        self.environment.setdefault(
            ohostedcons.EngineEnv.PROMPT_NON_OPERATIONAL,
            True
        )
        self.environment.setdefault(
            ohostedcons.EngineEnv.INSECURE_SSL,
            None
        )
        self._selinux_enabled = False

    @plugin.event(
        stage=plugin.Stages.STAGE_SETUP,
    )
    def _setup(self):
        self.command.detect('ip')

    @plugin.event(
        stage=plugin.Stages.STAGE_CUSTOMIZATION,
        after=(
            ohostedcons.Stages.DIALOG_TITLES_S_ENGINE,
        ),
        before=(
            ohostedcons.Stages.DIALOG_TITLES_E_ENGINE,
        ),
    )
    def _customization(self):
        interactive = (
            self.environment[ohostedcons.EngineEnv.APP_HOST_NAME] is None
        )
        while self.environment[ohostedcons.EngineEnv.APP_HOST_NAME] is None:
            hostname = self.dialog.queryString(
                name='APP_HOST_NAME',
                note=_(
                    'Enter the name which will be used to identify this host'
                    ' inside the Administrator Portal [@DEFAULT@]: '
                ),
                prompt=True,
                default='hosted_engine_%s' % self.environment[
                    ohostedcons.StorageEnv.HOST_ID
                ],
            )
            if hostname:
                self.environment[
                    ohostedcons.EngineEnv.APP_HOST_NAME
                ] = hostname
            else:
                if interactive:
                    self.logger.error(_('Please specify a host name'))
                else:
                    raise RuntimeError(
                        _('Empty host name not allowed')
                    )

        self._interactive_admin_pwd = (
            self.environment[ohostedcons.EngineEnv.ADMIN_PASSWORD] is None
        )
        while self.environment[ohostedcons.EngineEnv.ADMIN_PASSWORD] is None:
            password = self.dialog.queryString(
                name='ENGINE_ADMIN_PASSWORD',
                note=_(
                    "Enter 'admin@internal' user password that "
                    'will be used for accessing the Administrator Portal: '
                ),
                prompt=True,
                hidden=True,
            )
            if password:
                if not self._interactive_admin_pwd:
                    self.environment[
                        ohostedcons.EngineEnv.ADMIN_PASSWORD
                    ] = password
                else:
                    password_check = self.dialog.queryString(
                        name='ENGINE_ADMIN_PASSWORD',
                        note=_(
                            "Confirm 'admin@internal' user password: "
                        ),
                        prompt=True,
                        hidden=True,
                    )
                    if password == password_check:
                        self.environment[
                            ohostedcons.EngineEnv.ADMIN_PASSWORD
                        ] = password
                    else:
                        self.logger.error(_('Passwords do not match'))
            else:
                if self._interactive_admin_pwd:
                    self.logger.error(_('Please specify a password'))
                else:
                    raise RuntimeError(
                        _('Empty password not allowed for user admin')
                    )

    @plugin.event(
        stage=plugin.Stages.STAGE_VALIDATION,
    )
    def _validation(self):
        self._selinux_enabled = selinux.is_selinux_enabled()

    @plugin.event(
        stage=plugin.Stages.STAGE_CLOSEUP,
        after=(
            ohostedcons.Stages.ENGINE_ALIVE,
        ),
        name=ohostedcons.Stages.HOST_ADDED,
    )
    def _closeup(self):
        # TODO: refactor into shorter and simpler functions
        self._getCA()
        self._getSSH()
        cluster_name = None
        default_cluster_name = 'Default'
        valid = False
        fqdn = self.environment[
            ohostedcons.NetworkEnv.OVIRT_HOSTED_ENGINE_FQDN
        ]
        while not valid:
            try:
                self.logger.info(_('Connecting to the Engine'))
                insecure = False
                if self.environment[
                    ohostedcons.EngineEnv.INSECURE_SSL
                ]:
                    insecure = True
                engine_api = self._ovirtsdk_api.API(
                    url='https://{fqdn}/ovirt-engine/api'.format(
                        fqdn=fqdn,
                    ),
                    username='admin@internal',
                    password=self.environment[
                        ohostedcons.EngineEnv.ADMIN_PASSWORD
                    ],
                    ca_file=self.environment[
                        ohostedcons.EngineEnv.TEMPORARY_CERT_FILE
                    ],
                    insecure=insecure,
                )
                engine_api.clusters.list()
                valid = True
            except ovirtsdk.infrastructure.errors.RequestError as e:
                if e.status == 401:
                    if self._interactive_admin_pwd:
                        self.logger.error(
                            _(
                                'The engine API didn''t accepted '
                                'the administrator password you provided\n'
                                'Please enter it again to retry.'
                            )
                        )
                        self.environment[
                            ohostedcons.EngineEnv.ADMIN_PASSWORD
                        ] = self.dialog.queryString(
                            name='ENGINE_ADMIN_PASSWORD',
                            note=_(
                                'Enter ''admin@internal'' user password that '
                                'will be used for accessing '
                                'the Administrator Portal: '
                            ),
                            prompt=True,
                            hidden=True,
                        )
                    else:
                        raise RuntimeError(
                            _(
                                'The engine API didn''t accepted '
                                'the administrator password you provided\n'
                            )
                        )
                else:
                    self.logger.error(
                        _(
                            'Cannot connect to engine APIs on {fqdn}:\n'
                            '{details}\n'
                        ).format(
                            fqdn=fqdn,
                            details=e.detail,
                        )
                    )
                    raise RuntimeError(
                        _(
                            'Cannot connect to engine APIs on {fqdn}'
                        ).format(
                            fqdn=fqdn,
                        )
                    )

        added_to_cluster = False
        while not added_to_cluster:
            try:
                cluster_name = self.environment[
                    ohostedcons.EngineEnv.HOST_CLUSTER_NAME
                ]
                self.logger.debug(
                    "Getting the list of available clusters via engine's APIs"
                )
                if cluster_name is not None:
                    if cluster_name not in [
                        c.get_name()
                        for c in engine_api.clusters.list()
                    ]:
                        raise RuntimeError(
                            _(
                                'Specified cluster does not exist: {cluster}'
                            ).format(
                                cluster=cluster_name,
                            )
                        )
                else:
                    cluster_l = [
                        c.get_name()
                        for c in engine_api.clusters.list()
                    ]
                    cluster_name = (
                        default_cluster_name if default_cluster_name in
                        cluster_l else cluster_l[0]
                    )
                    cluster_name = self.dialog.queryString(
                        name='cluster_name',
                        note=_(
                            'Enter the name of the cluster to which '
                            'you want to add the host (@VALUES@) '
                            '[@DEFAULT@]: '
                        ),
                        prompt=True,
                        default=cluster_name,
                        validValues=cluster_l,
                    )
                    self.environment[
                        ohostedcons.EngineEnv.HOST_CLUSTER_NAME
                    ] = cluster_name
                cluster = engine_api.clusters.get(cluster_name)

                conn = self.environment[ohostedcons.VDSMEnv.VDS_CLI]
                net_info = netinfo.NetInfo(vds_info.capabilities(conn))
                bridge_port = self.environment[
                    ohostedcons.NetworkEnv.BRIDGE_IF
                ]
                if bridge_port in net_info.vlans:
                    self.logger.debug(
                        "Updating engine's management network to be vlanned"
                    )
                    vlan_id = net_info.vlans[bridge_port]['vlanid']
                    self.logger.debug(
                        "Getting engine's management network via engine's APIs"
                    )
                    mgmt_network = cluster.networks.get(
                        name=self.environment[
                            ohostedcons.NetworkEnv.BRIDGE_NAME]
                    )
                    mgmt_network.set_vlan(
                        self._ovirtsdk_xml.params.VLAN(id=vlan_id)
                    )
                    mgmt_network.update()

                # Configuring the cluster for Hyper Converged support if
                # enabled
                if self.environment[
                    ohostedcons.StorageEnv.GLUSTER_PROVISIONING_ENABLED
                ]:
                    cluster.set_gluster_service(True)
                    cluster.update()
                    cluster = engine_api.clusters.get(cluster_name)

                self.logger.debug('Adding the host to the cluster')

                engine_api.hosts.add(
                    self._ovirtsdk_xml.params.Host(
                        name=self.environment[
                            ohostedcons.EngineEnv.APP_HOST_NAME
                        ],
                        # Note that the below is required for compatibility
                        # with vdsm-generated pki. See bz 1178535.
                        # TODO: Make it configurable like engine fqdn.
                        address=socket.gethostname(),
                        reboot_after_installation=False,
                        cluster=cluster,
                        ssh=self._ovirtsdk_xml.params.SSH(
                            authentication_method='publickey',
                            port=self.environment[
                                ohostedcons.NetworkEnv.SSHD_PORT
                            ],
                        ),
                        override_iptables=self.environment[
                            otopicons.NetEnv.IPTABLES_ENABLE
                        ],
                    )
                )
                added_to_cluster = True
            except ovirtsdk.infrastructure.errors.RequestError as e:
                self.logger.debug(
                    'Cannot add the host to cluster {cluster}'.format(
                        cluster=cluster_name,
                    ),
                    exc_info=True,
                )
                self.logger.error(
                    _(
                        'Cannot automatically add the host '
                        'to cluster {cluster}:\n{details}\n'
                    ).format(
                        cluster=cluster_name,
                        details=e.detail
                    )
                )
                while not check_liveliness.manualSetupDispatcher(
                    self,
                    check_liveliness.MSD_FURTHER_ACTIONS,
                    fqdn
                ):
                    pass

        up = self._wait_host_ready(
            engine_api,
            self.environment[ohostedcons.EngineEnv.APP_HOST_NAME]
        )
        # TODO: host-deploy restarted vdscli so we need to
        # connect again
        if not up:
            self.logger.error(
                _(
                    'Unable to add {host} to the manager'
                ).format(
                    host=self.environment[
                        ohostedcons.EngineEnv.APP_HOST_NAME
                    ],
                )
            )
        else:
            # This works only if the host is up.
            self.logger.debug('Setting CPU for the cluster')
            try:
                cluster, cpu = self._wait_cluster_cpu_ready(
                    engine_api,
                    cluster_name
                )
                self.logger.debug(cpu.__dict__)
                cpu.set_id(
                    self.environment[ohostedcons.VDSMEnv.ENGINE_CPU]
                )
                cluster.set_cpu(cpu)
                cluster.update()
            except ovirtsdk.infrastructure.errors.RequestError as e:
                self.logger.debug(
                    'Cannot set CPU level of cluster {cluster}'.format(
                        cluster=cluster_name,
                    ),
                    exc_info=True,
                )
                self.logger.error(
                    _(
                        'Cannot automatically set CPU level '
                        'of cluster {cluster}:\n{details}\n'
                    ).format(
                        cluster=cluster_name,
                        details=e.detail
                    )
                )
        engine_api.disconnect()

    @plugin.event(
        stage=plugin.Stages.STAGE_CLEANUP,
    )
    def _cleanup(self):
        cert = self.environment[ohostedcons.EngineEnv.TEMPORARY_CERT_FILE]
        if cert is not None and os.path.exists(cert):
            os.unlink(cert)


# vim: expandtab tabstop=4 shiftwidth=4
