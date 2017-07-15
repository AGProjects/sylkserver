
"""SIP SIMPLE SDK settings extensions"""

import os

from application import log
from sipsimple.account import MSRPSettings as AccountMSRPSettings, NATTraversalSettings as AccountNATTraversalSettings
from sipsimple.account import RTPSettings as AccountRTPSettings, SIPSettings as AccountSIPSettings, TLSSettings as AccountTLSSettings, SRTPEncryptionSettings as AccountSRTPEncryptionSettings
from sipsimple.account import MessageSummarySettings as AccountMessageSummarySettings, PresenceSettings as AccountPresenceSettingss, XCAPSettings as AccountXCAPSettings
from sipsimple.configuration import CorrelatedSetting, Setting, SettingsObjectExtension
from sipsimple.configuration.datatypes import MSRPConnectionModel, MSRPTransport, NonNegativeInteger, PortRange, SampleRate, SIPTransportList, SRTPKeyNegotiation
from sipsimple.configuration.settings import AudioSettings, EchoCancellerSettings, FileTransferSettings, LogsSettings, RTPSettings, SIPSettings, TLSSettings

from sylk import __version__ as server_version
from sylk.configuration import ServerConfig, SIPConfig, MSRPConfig, RTPConfig
from sylk.configuration.datatypes import AudioCodecs, Path, Port, SIPProxyAddress


__all__ = 'AccountExtension', 'BonjourAccountExtension', 'SylkServerSettingsExtension'


# Account settings extensions

class AccountMessageSummarySettingsExtension(AccountMessageSummarySettings):
    enabled = Setting(type=bool, default=False)


class AccountMSRPSettingsExtension(AccountMSRPSettings):
    transport = Setting(type=MSRPTransport, default='tls' if MSRPConfig.use_tls else 'tcp')
    connection_model = Setting(type=MSRPConnectionModel, default='relay' if ServerConfig.enable_bonjour else 'acm')


class AccountNATTraversalSettingsExtension(AccountNATTraversalSettings):
    use_ice = Setting(type=bool, default=SIPConfig.enable_ice)
    use_msrp_relay_for_outbound = Setting(type=bool, default=False)


class AccountPresenceSettingssExtension(AccountPresenceSettingss):
    enabled = Setting(type=bool, default=False)


if RTPConfig.srtp_encryption == 'disabled':
    # doesn't matter because it's disabled
    srtp_key_negotiation = 'opportunistic'
elif RTPConfig.srtp_encryption == 'sdes':
    srtp_key_negotiation = 'sdes_optional'
else:
    srtp_key_negotiation = RTPConfig.srtp_encryption


class AccountSRTPEncryptionSettingsExtension(AccountSRTPEncryptionSettings):
    enabled = Setting(type=bool, default=RTPConfig.srtp_encryption!='disabled')
    key_negotiation = Setting(type=SRTPKeyNegotiation, default=srtp_key_negotiation)


class AccountRTPSettingsExtension(AccountRTPSettings):
    audio_codec_list = Setting(type=AudioCodecs, default=None, nillable=True)
    encryption = AccountSRTPEncryptionSettingsExtension


class AccountSIPSettingsExtension(AccountSIPSettings):
    register = Setting(type=bool, default=False)
    outbound_proxy = Setting(type=SIPProxyAddress, default=SIPConfig.outbound_proxy, nillable=True)


account_cert = ServerConfig.certificate
if account_cert is not None and not os.path.isfile(account_cert):
    account_cert = None


class AccountTLSSettingsExtension(AccountTLSSettings):
    certificate = Setting(type=Path, default=account_cert, nillable=True)
    verify_server = Setting(type=bool, default=ServerConfig.verify_server)


class AccountXCAPSettingsExtension(AccountXCAPSettings):
    enabled = Setting(type=bool, default=False)


class AccountExtension(SettingsObjectExtension):
    enabled = Setting(type=bool, default=True)

    message_summary = AccountMessageSummarySettingsExtension
    msrp = AccountMSRPSettingsExtension
    nat_traversal = AccountNATTraversalSettingsExtension
    presence = AccountPresenceSettingssExtension
    rtp = AccountRTPSettingsExtension
    sip = AccountSIPSettingsExtension
    tls = AccountTLSSettingsExtension
    xcap = AccountXCAPSettingsExtension


class BonjourAccountExtension(SettingsObjectExtension):
    enabled = Setting(type=bool, default=False)


# General settings extensions

class EchoCancellerSettingsExtension(EchoCancellerSettings):
    enabled = Setting(type=bool, default=False)
    tail_length = Setting(type=NonNegativeInteger, default=0)


class AudioSettingsExtension(AudioSettings):
    input_device = Setting(type=str, default=None, nillable=True)
    output_device = Setting(type=str, default=None, nillable=True)
    sample_rate = Setting(type=SampleRate, default=RTPConfig.sample_rate)
    echo_canceller = EchoCancellerSettings


class FileTransferSettingsExtension(FileTransferSettings):
    directory = Setting(type=Path, default=Path(os.path.join(ServerConfig.spool_dir.normalized, 'file_transfers')))


class LogsSettingsExtension(LogsSettings):
    directory = Setting(type=Path, default=ServerConfig.trace_dir)
    trace_sip = Setting(type=bool, default=ServerConfig.trace_sip)
    trace_msrp = Setting(type=bool, default=ServerConfig.trace_msrp)
    trace_pjsip = Setting(type=bool, default=ServerConfig.trace_core)


class RTPSettingsExtension(RTPSettings):
    audio_codec_list = Setting(type=AudioCodecs, default=RTPConfig.audio_codecs)
    port_range = Setting(type=PortRange, default=PortRange(RTPConfig.port_range.start, RTPConfig.port_range.end))
    timeout = Setting(type=NonNegativeInteger, default=RTPConfig.timeout)


ca_file = ServerConfig.ca_file
if ca_file is not None and not os.path.isfile(ca_file):
    ca_file = None


class TLSSettingsExtension(TLSSettings):
    ca_list = Setting(type=Path, default=ca_file, nillable=True)


def sip_port_validator(port, sibling_port):
    if port == sibling_port != 0:
        raise ValueError("the TCP and TLS ports must be different")

transport_list = []
if SIPConfig.local_udp_port is not None:
    transport_list.append('udp')
if SIPConfig.local_tcp_port is not None:
    transport_list.append('tcp')
tls_port = SIPConfig.local_tls_port
if tls_port is not None and None in (ca_file, account_cert):
    log.warning('Cannot enable TLS because the CA or the certificate are not specified')
    tls_port = None
if tls_port is not None:
    transport_list.append('tls')


class SIPSettingsExtension(SIPSettings):
    udp_port = Setting(type=Port, default=SIPConfig.local_udp_port, nillable=True)
    tcp_port = CorrelatedSetting(type=Port, sibling='tls_port', validator=sip_port_validator, default=SIPConfig.local_tcp_port, nillable=True)
    tls_port = CorrelatedSetting(type=Port, sibling='tcp_port', validator=sip_port_validator, default=tls_port, nillable=True)
    transport_list = Setting(type=SIPTransportList, default=transport_list)


class SylkServerSettingsExtension(SettingsObjectExtension):
    user_agent = Setting(type=str, default='SylkServer-%s' % server_version)

    audio = AudioSettingsExtension
    file_transfer = FileTransferSettingsExtension
    logs = LogsSettingsExtension
    rtp = RTPSettingsExtension
    sip = SIPSettingsExtension
    tls = TLSSettingsExtension

