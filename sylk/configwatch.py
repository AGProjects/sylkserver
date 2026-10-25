
"""Hot-reload of the handful of settings that are safe to change in a running
SylkServer, without a restart.

A small reactor timer polls the config files and, when one changes on disk,
re-reads only an explicit allow-list of keys and applies them live. Anything
not listed here is bound into the SIP engine, the listening sockets or the TLS
contexts at startup and still requires a restart; the watcher never touches it.

Hot keys:

    config.ini         [Server]  trace_dns, trace_sip, trace_msrp, trace_core,
                                  trace_notifications
    config.ini         [SIP]     trusted_peers (the source-IP ACL)
    webrtcgateway.ini  [General] trace_client
    webrtcgateway.ini  [Janus]   trace_janus

The trace flags are pushed to the TraceLogManager (which also owns the
webrtcgateway client/janus trace loggers once that application is loaded).
trusted_peers is re-read into the AuthorizationHandler's cached copy and takes
effect on the next request.
"""

from application import log
from application.configuration import ConfigFile
from application.python.types import Singleton
from twisted.internet import task

from sylk.configuration import SIPConfig
from sylk.log import TraceLogManager


# (config file, section, key, trace logger name)
_TRACE_FLAGS = [
    ('config.ini',        'Server',  'trace_dns',           'dns_trace'),
    ('config.ini',        'Server',  'trace_sip',           'sip_trace'),
    ('config.ini',        'Server',  'trace_msrp',          'msrp_trace'),
    ('config.ini',        'Server',  'trace_core',          'core_trace'),
    ('config.ini',        'Server',  'trace_notifications', 'notification_trace'),
    ('webrtcgateway.ini', 'General', 'trace_client',        'webrtc_client_trace'),
    ('webrtcgateway.ini', 'Janus',   'trace_janus',         'webrtc_janus_trace'),
]

# config.ini [SIP] keys handled by dedicated appliers
_SIP_KEYS = [('SIP', 'trusted_peers')]


class ConfigWatcher(object, metaclass=Singleton):
    interval = 5   # seconds between mtime checks

    def __init__(self):
        self._call = task.LoopingCall(self._poll)
        self._timestamps = {}   # cfgfile -> last ConfigFile.timestamp
        self._raw = {}          # (cfgfile, section, key) -> last raw string seen

    @property
    def _files(self):
        files = {name for (name, _s, _k, _n) in _TRACE_FLAGS}
        files.add('config.ini')
        return files

    def start(self):
        # Seed timestamps and the raw snapshot from the current on-disk config
        # so the already-applied startup configuration is not re-applied on the
        # first poll; only genuine edits act from here on.
        for name in self._files:
            self._timestamps[name] = ConfigFile(name).timestamp
        self._snapshot_raw()
        self._call.start(self.interval, now=False)
        log.info('ConfigWatcher: polling %s every %ds for hot-reloadable changes'
                 % (', '.join(sorted(self._files)), self.interval))

    def stop(self):
        if self._call.running:
            self._call.stop()

    # -- raw-value tracking (diff per key so one edited key doesn't re-apply others) --

    @staticmethod
    def _raw_get(cfg, section, key):
        try:
            return cfg.parser.get(section, key)
        except Exception:
            return None

    def _snapshot_raw(self):
        for name in self._files:
            cfg = ConfigFile(name)
            for (f, section, key, _n) in _TRACE_FLAGS:
                if f == name:
                    self._raw[(name, section, key)] = self._raw_get(cfg, section, key)
            if name == 'config.ini':
                for section, key in _SIP_KEYS:
                    self._raw[(name, section, key)] = self._raw_get(cfg, section, key)

    def _changed(self, name, cfg, section, key):
        raw = self._raw_get(cfg, section, key)
        if raw == self._raw.get((name, section, key)):
            return False, raw
        self._raw[(name, section, key)] = raw
        return True, raw

    # -- polling --

    def _poll(self):
        for name in self._files:
            cfg = ConfigFile(name)
            if cfg.timestamp == self._timestamps.get(name):
                continue
            self._timestamps[name] = cfg.timestamp
            try:
                self._apply_file(name, cfg)
            except Exception:
                log.exception('ConfigWatcher: error applying changes from %s' % name)

    def _apply_file(self, name, cfg):
        mgr = TraceLogManager()
        for (f, section, key, logger_name) in _TRACE_FLAGS:
            if f != name:
                continue
            changed, _raw = self._changed(name, cfg, section, key)
            if not changed:
                continue
            desired = bool(cfg.get_setting(section, key, type=bool, default=False))
            try:
                mgr.set_trace(logger_name, desired)
            except ValueError:
                continue   # logger not registered (owning application not loaded)
            log.info('ConfigWatcher: %s %s -> %s' % (name, key, 'on' if desired else 'off'))
        if name == 'config.ini':
            self._apply_acl(cfg)

    # -- individual appliers --

    def _apply_acl(self, cfg):
        changed, raw = self._changed('config.ini', cfg, 'SIP', 'trusted_peers')
        if not changed:
            return
        setting = SIPConfig.__settings__['trusted_peers']
        SIPConfig.trusted_peers = setting.type(raw) if raw is not None else setting.value
        from sylk.applications import IncomingRequestHandler
        IncomingRequestHandler().authorization_handler.trusted_peers = SIPConfig.trusted_peers
        log.info('ConfigWatcher: SIP trusted_peers (ACL) -> %s' % (SIPConfig.trusted_peers,))
