#
# ovirt-hosted-engine-setup -- ovirt hosted engine setup
# Copyright (C) 2016 Red Hat, Inc.
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

"""Connect to Engine API"""


import gettext

from ovirt_hosted_engine_setup import constants as ohostedcons

import ovirtsdk.api
import ovirtsdk.infrastructure.errors
import ovirtsdk.xml


def _(m):
    return gettext.dgettext(message=m, domain='ovirt-hosted-engine-setup')


def get_engine_api(
    base,
    timeout=None,
):
    '''
    :param base: Reference to caller.
    :param timeout: Engine API timeout.
    '''
    valid = False
    engine_api = None
    fqdn = base.environment[
        ohostedcons.NetworkEnv.OVIRT_HOSTED_ENGINE_FQDN
    ]
    while not valid:
        try:
            base.logger.info(_('Connecting to Engine'))
            insecure = False
            if base.environment[
                ohostedcons.EngineEnv.INSECURE_SSL
            ]:
                insecure = True
            engine_api = ovirtsdk.api.API(
                url='https://{fqdn}/ovirt-engine/api'.format(
                    fqdn=fqdn,
                ),
                username='admin@internal',
                password=base.environment[
                    ohostedcons.EngineEnv.ADMIN_PASSWORD
                ],
                ca_file=base.environment[
                    ohostedcons.EngineEnv.TEMPORARY_CERT_FILE
                ],
                insecure=insecure,
                timeout=timeout if timeout else
                ohostedcons.Defaults.DEFAULT_ENGINE_SETUP_TIMEOUT,
            )
            engine_api.clusters.list()
            valid = True
        except ovirtsdk.infrastructure.errors.RequestError as e:
            if e.status == 401:
                if base.environment[
                    ohostedcons.EngineEnv.INTERACTIVE_ADMIN_PASSWORD
                ]:
                    base.logger.error(
                        _(
                            'The Engine API didn''t accept '
                            'the administrator password you provided.\n'
                            'Please enter it again to retry.'
                        )
                    )
                    base.environment[
                        ohostedcons.EngineEnv.ADMIN_PASSWORD
                    ] = base.dialog.queryString(
                        name='ENGINE_ADMIN_PASSWORD',
                        note=_(
                            'Enter engine admin password: '
                        ),
                        prompt=True,
                        hidden=True,
                    )
                else:
                    raise RuntimeError(
                        _(
                            'The Engine API didn''t accept '
                            'the administrator password you provided\n'
                        )
                    )
            else:
                base.logger.error(
                    _(
                        'Cannot connect to Engine API on {fqdn}:\n'
                        '{details}\n'
                    ).format(
                        fqdn=fqdn,
                        details=e.detail,
                    )
                )
                raise RuntimeError(
                    _(
                        'Cannot connect to Engine API on {fqdn}'
                    ).format(
                        fqdn=fqdn,
                    )
                )
    return engine_api


# vim: expandtab tabstop=4 shiftwidth=4
