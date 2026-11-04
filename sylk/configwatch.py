
"""Hot-reload of the handful of settings that are safe to change in a running
SylkServer, without a restart.

A small reactor timer polls the config files and, when one changes on disk,
re-reads only an explicit allow-list of keys and applies them live. Anything
not listed here is bound into the SIP engine, the listening sockets or the TLS
contexts at startup and still requires a restart; the watcher never touches it.

Hot keys:

    config.ini         [Server]  log_dns, log_sip, log_msrp, log_core,
                                  log_notifications (or their old trace_* names)
    config.ini         [SIP]     trusted_peers (the source-IP ACL)
    webrtcgateway.ini  [General] log_webrtc_client_api
    webrtcgateway.ini  [Janus]   log_janus_api

Settings renamed with sylk.configuration.rename_settings are also found under
their old name (trace_*) when the new name is not set.

Applications can add their own hot keys with ConfigWatcher().register(), e.g.
webrtcgateway's log_messaging, log_addressbook, log_file_transfers and
log_connections (and the matching log files).

The trace flags are pushed to the TraceLogManager (which also owns the
webrtcgateway client/janus trace loggers once that application is loaded).
trusted_peers is re-read into the AuthorizationHandler's cached copy and takes
effect on the next request.
"""

from application import log
from application.configuration import ConfigFile, datatypes
from application.python.types import Singleton
from twisted.internet import task

from sylk.configuration import RENAMED_SETTINGS, SIPConfig
from sylk.log import TraceLogManager


# (config file, section, key, trace logger name)
_TRACE_FLAGS = [
    ('config.ini',        'Server',  'log_dns',             'dns_trace'),
    ('config.ini',        'Server',  'log_sip',             'sip_trace'),
    ('config.ini',        'Server',  'log_msrp',            'msrp_trace'),
    ('config.ini',        'Server',  'log_core',            'core_trace'),
    ('config.ini',        'Server',  'log_notifications',   'notification_trace'),
    ('webrtcgateway.ini', 'General', 'log_webrtc_client_api', 'webrtc_client_trace'),
    ('webrtcgateway.ini', 'Janus',   'log_janus_api',       'webrtc_janus_trace'),
]

# config.ini [SIP] keys handled by dedicated appliers
_SIP_KEYS = [('SIP', 'trusted_peers')]


class ConfigWatcher(object, metaclass=Singleton):
    interval = 5   # seconds between mtime checks

    def __init__(self):
        self._call = task.LoopingCall(self._poll)
        self._timestamps = {}   # cfgfile -> last ConfigFile.timestamp
        self._raw = {}          # (cfgfile, section, key) -> last raw string seen
        self._callbacks = []    # (cfgfile, section, key, callback) added with register()

    @property
    def _files(self):
        files = {name for (name, _s, _k, _n) in _TRACE_FLAGS}
        files.add('config.ini')
        files.update(name for (name, _s, _k, _c) in self._callbacks)
        return files

    def register(self, cfgfile, section, key, callback):
        """Call callback(raw_value) when key changes on disk, raw_value being
        None when the key was removed. The value at registration time counts as
        already applied."""
        cfg = ConfigFile(cfgfile)
        self._callbacks.append((cfgfile, section, key, callback))
        self._raw[(cfgfile, section, key)] = self._raw_get(cfg, section, key)
        self._timestamps.setdefault(cfgfile, cfg.timestamp)

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
        keys = [key]
        if (cfg.filename, section, key) in RENAMED_SETTINGS:
            keys.append(RENAMED_SETTINGS[(cfg.filename, section, key)])   # the old name, used when the new one is not set
        for name in keys:
            try:
                return cfg.parser.get(section, name)
            except Exception:
                pass
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
            for (f, section, key, _c) in self._callbacks:
                if f == name:
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
            try:
                desired = bool(datatypes.Boolean(_raw)) if _raw is not None else False
            except (TypeError, ValueError):
                log.warning('ConfigWatcher: ignoring invalid %s %s value %r' % (name, key, _raw))
                continue
            try:
                mgr.set_trace(logger_name, desired)
            except ValueError:
                continue   # logger not registered (owning application not loaded)
            log.info('ConfigWatcher: %s %s -> %s' % (name, key, 'on' if desired else 'off'))
        if name == 'config.ini':
            self._apply_acl(cfg)
        for (f, section, key, callback) in self._callbacks:
            if f != name:
                continue
            changed, raw = self._changed(name, cfg, section, key)
            if changed:
                try:
                    callback(raw)
                except Exception:
                    log.exception('ConfigWatcher: error applying %s %s' % (name, key))

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
