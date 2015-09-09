/*
 * sylkrtc.js v0.1.2
 * SylkServer WebRTC Gateway client library
 * Copyright 2015 AG Projects
 * License MIT
 */

(function(f){if(typeof exports==="object"&&typeof module!=="undefined"){module.exports=f()}else if(typeof define==="function"&&define.amd){define([],f)}else{var g;if(typeof window!=="undefined"){g=window}else if(typeof global!=="undefined"){g=global}else if(typeof self!=="undefined"){g=self}else{g=this}g.sylkrtc = f()}})(function(){var define,module,exports;return (function e(t,n,r){function s(o,u){if(!n[o]){if(!t[o]){var a=typeof require=="function"&&require;if(!u&&a)return a(o,!0);if(i)return i(o,!0);var f=new Error("Cannot find module '"+o+"'");throw f.code="MODULE_NOT_FOUND",f}var l=n[o]={exports:{}};t[o][0].call(l.exports,function(e){var n=t[o][1][e];return s(n?n:e)},l,l.exports,e,t,n,r)}return n[o].exports}var i=typeof require=="function"&&require;for(var o=0;o<r.length;o++)s(r[o]);return s})({1:[function(require,module,exports){
'use strict';

Object.defineProperty(exports, '__esModule', {
    value: true
});

var _createClass = (function () {
    function defineProperties(target, props) {
        for (var i = 0; i < props.length; i++) {
            var descriptor = props[i];descriptor.enumerable = descriptor.enumerable || false;descriptor.configurable = true;if ('value' in descriptor) descriptor.writable = true;Object.defineProperty(target, descriptor.key, descriptor);
        }
    }return function (Constructor, protoProps, staticProps) {
        if (protoProps) defineProperties(Constructor.prototype, protoProps);if (staticProps) defineProperties(Constructor, staticProps);return Constructor;
    };
})();

var _get = function get(_x2, _x3, _x4) {
    var _again = true;_function: while (_again) {
        var object = _x2,
            property = _x3,
            receiver = _x4;desc = parent = getter = undefined;_again = false;if (object === null) object = Function.prototype;var desc = Object.getOwnPropertyDescriptor(object, property);if (desc === undefined) {
            var parent = Object.getPrototypeOf(object);if (parent === null) {
                return undefined;
            } else {
                _x2 = parent;_x3 = property;_x4 = receiver;_again = true;continue _function;
            }
        } else if ('value' in desc) {
            return desc.value;
        } else {
            var getter = desc.get;if (getter === undefined) {
                return undefined;
            }return getter.call(receiver);
        }
    }
};

function _interopRequireDefault(obj) {
    return obj && obj.__esModule ? obj : { 'default': obj };
}

function _classCallCheck(instance, Constructor) {
    if (!(instance instanceof Constructor)) {
        throw new TypeError('Cannot call a class as a function');
    }
}

function _inherits(subClass, superClass) {
    if (typeof superClass !== 'function' && superClass !== null) {
        throw new TypeError('Super expression must either be null or a function, not ' + typeof superClass);
    }subClass.prototype = Object.create(superClass && superClass.prototype, { constructor: { value: subClass, enumerable: false, writable: true, configurable: true } });if (superClass) subClass.__proto__ = superClass;
}

var _debug = require('debug');

var _debug2 = _interopRequireDefault(_debug);

var _blueimpMd5 = require('blueimp-md5');

var _events = require('events');

var _call = require('./call');

var DEBUG = (0, _debug2['default'])('sylkrtc:Account');

var Account = (function (_EventEmitter) {
    _inherits(Account, _EventEmitter);

    function Account(id, password, connection) {
        _classCallCheck(this, Account);

        if (id.indexOf('@') === -1) {
            throw new Error('Invalid account id specified');
        }
        _get(Object.getPrototypeOf(Account.prototype), 'constructor', this).call(this);
        var username = id.substring(0, id.indexOf('@'));
        var domain = id.substring(id.indexOf('@') + 1);
        this._id = id;
        this._password = (0, _blueimpMd5.md5)(username + ':' + domain + ':' + password);
        this._connection = connection;
        this._registrationState = null;
        this._calls = new Map();
    }

    _createClass(Account, [{
        key: 'register',
        value: function register() {
            var _this = this;

            var req = {
                sylkrtc: 'account-register',
                account: this._id
            };
            this._sendRequest(req, function (error) {
                if (error) {
                    DEBUG('Register error: %s', error);
                    var oldState = _this._registrationState;
                    var newState = 'failed';
                    var data = { reason: error.toString() };
                    _this._registrationState = newState;
                    _this.emit('registrationStateChanged', oldState, newState, data);
                }
            });
        }
    }, {
        key: 'unregister',
        value: function unregister() {
            var _this2 = this;

            var req = {
                sylkrtc: 'account-unregister',
                account: this._id
            };
            this._sendRequest(req, function (error) {
                if (error) {
                    DEBUG('Unregister error: %s', error);
                }
                var oldState = _this2._registrationState;
                var newState = null;
                _this2._registrationState = newState;
                _this2.emit('registrationStateChanged', oldState, newState, {});
            });
        }
    }, {
        key: 'call',
        value: function call(uri) {
            var options = arguments.length <= 1 || arguments[1] === undefined ? {} : arguments[1];

            var call = new _call.Call(this);
            call._initOutgoing(uri, options);
            this._calls.set(call.id, call);
            this.emit('outgoingCall', call);
            return call;
        }
    }, {
        key: '_handleEvent',

        // Private API

        value: function _handleEvent(message) {
            DEBUG('Received account event: %s', message.event);
            switch (message.event) {
                case 'registration_state':
                    var oldState = this._registrationState;
                    var newState = message.data.state;
                    var data = {};
                    this._registrationState = newState;
                    if (newState === 'failed') {
                        data.reason = message.data.reason;
                    }
                    this.emit('registrationStateChanged', oldState, newState, data);
                    break;
                case 'incoming_session':
                    var call = new _call.Call(this);
                    call._initIncoming(message.session, message.data.originator, message.data.sdp);
                    this._calls.set(call.id, call);
                    this.emit('incomingCall', call);
                    break;
                default:
                    break;
            }
        }
    }, {
        key: '_sendRequest',
        value: function _sendRequest(req, cb) {
            this._connection._sendRequest(req, cb);
        }
    }, {
        key: 'id',
        get: function get() {
            return this._id;
        }
    }, {
        key: 'password',
        get: function get() {
            return this._password;
        }
    }, {
        key: 'registrationState',
        get: function get() {
            return this._registrationState;
        }
    }]);

    return Account;
})(_events.EventEmitter);

exports.Account = Account;

},{"./call":2,"blueimp-md5":5,"debug":9,"events":6}],2:[function(require,module,exports){
'use strict';

Object.defineProperty(exports, '__esModule', {
    value: true
});

var _createClass = (function () {
    function defineProperties(target, props) {
        for (var i = 0; i < props.length; i++) {
            var descriptor = props[i];descriptor.enumerable = descriptor.enumerable || false;descriptor.configurable = true;if ('value' in descriptor) descriptor.writable = true;Object.defineProperty(target, descriptor.key, descriptor);
        }
    }return function (Constructor, protoProps, staticProps) {
        if (protoProps) defineProperties(Constructor.prototype, protoProps);if (staticProps) defineProperties(Constructor, staticProps);return Constructor;
    };
})();

var _get = function get(_x3, _x4, _x5) {
    var _again = true;_function: while (_again) {
        var object = _x3,
            property = _x4,
            receiver = _x5;desc = parent = getter = undefined;_again = false;if (object === null) object = Function.prototype;var desc = Object.getOwnPropertyDescriptor(object, property);if (desc === undefined) {
            var parent = Object.getPrototypeOf(object);if (parent === null) {
                return undefined;
            } else {
                _x3 = parent;_x4 = property;_x5 = receiver;_again = true;continue _function;
            }
        } else if ('value' in desc) {
            return desc.value;
        } else {
            var getter = desc.get;if (getter === undefined) {
                return undefined;
            }return getter.call(receiver);
        }
    }
};

function _interopRequireDefault(obj) {
    return obj && obj.__esModule ? obj : { 'default': obj };
}

function _classCallCheck(instance, Constructor) {
    if (!(instance instanceof Constructor)) {
        throw new TypeError('Cannot call a class as a function');
    }
}

function _inherits(subClass, superClass) {
    if (typeof superClass !== 'function' && superClass !== null) {
        throw new TypeError('Super expression must either be null or a function, not ' + typeof superClass);
    }subClass.prototype = Object.create(superClass && superClass.prototype, { constructor: { value: subClass, enumerable: false, writable: true, configurable: true } });if (superClass) subClass.__proto__ = superClass;
}

var _debug = require('debug');

var _debug2 = _interopRequireDefault(_debug);

var _nodeUuid = require('node-uuid');

var _nodeUuid2 = _interopRequireDefault(_nodeUuid);

var _rtcninja = require('rtcninja');

var _rtcninja2 = _interopRequireDefault(_rtcninja);

var _events = require('events');

var DEBUG = (0, _debug2['default'])('sylkrtc:Call');

var Call = (function (_EventEmitter) {
    _inherits(Call, _EventEmitter);

    function Call(account) {
        _classCallCheck(this, Call);

        _get(Object.getPrototypeOf(Call.prototype), 'constructor', this).call(this);
        this._account = account;
        this._id = null;
        this._direction = null;
        this._pc = null;
        this._state = null;
        this._terminated = false;
        this._incomingSdp = null;
        this._remoteIdentity = null;
    }

    _createClass(Call, [{
        key: 'getLocalStreams',
        value: function getLocalStreams() {
            if (this._pc !== null) {
                return this._pc.getLocalStreams();
            } else {
                return [];
            }
        }
    }, {
        key: 'getRemoteStreams',
        value: function getRemoteStreams() {
            if (this._pc !== null) {
                return this._pc.getRemoteStreams();
            } else {
                return [];
            }
        }
    }, {
        key: 'answer',
        value: function answer() {
            var options = arguments.length <= 0 || arguments[0] === undefined ? {} : arguments[0];

            if (this._state !== 'incoming') {
                throw new Error('Call is not in the incoming state: ' + this._state);
            }

            var self = this;
            var pcConfig = options.pcConfig || { iceServers: [] };
            var mediaConstraints = options.mediaConstraints || { audio: true, video: true };
            var answerOptions = options.answerOptions;

            // Create the RTCPeerConnection
            this._initRTCPeerConnection(pcConfig);

            // Get the user media
            _rtcninja2['default'].getUserMedia(mediaConstraints, userMediaSucceeded, userMediaFailed);

            function userMediaSucceeded(stream) {
                // adding a local stream doesn't trigger the 'onaddstream' callback
                self._pc.addStream(stream);
                self.emit('localStreamAdded', stream);

                self._pc.setRemoteDescription(new _rtcninja2['default'].RTCSessionDescription({ type: 'offer', sdp: self._incomingSdp }),
                // success
                function () {
                    self._createLocalSDP('answer', answerOptions,
                    // success
                    function (sdp) {
                        DEBUG('Local SDP: %s', sdp);
                        self._sendAnswer(sdp);
                    },
                    // failure
                    function (error) {
                        DEBUG('Error creating local SDP: %s', error);
                        self.terminate();
                    });
                },
                // failure
                function (error) {
                    DEBUG('Error setting remote description: %s', error);
                    self.terminate();
                });
            }

            function userMediaFailed(error) {
                DEBUG('Error getting user media: %s', error);
                self.terminate();
            }
        }
    }, {
        key: 'terminate',
        value: function terminate() {
            if (this._terminated) {
                return;
            }

            this._sendTerminate();
        }
    }, {
        key: '_initOutgoing',

        // Private API

        value: function _initOutgoing(uri) {
            var options = arguments.length <= 1 || arguments[1] === undefined ? {} : arguments[1];

            if (uri.indexOf('@') === -1) {
                throw new Error('Invalid URI');
            }

            this._id = _nodeUuid2['default'].v4();
            this._direction = 'outgoing';
            this._remoteIdentity = uri;

            var self = this;
            var pcConfig = options.pcConfig || { iceServers: [] };
            var mediaConstraints = options.mediaConstraints || { audio: true, video: true };
            var offerOptions = options.offerOptions;

            // Create the RTCPeerConnection
            this._initRTCPeerConnection(pcConfig);

            // Get the user media
            _rtcninja2['default'].getUserMedia(mediaConstraints, userMediaSucceeded, userMediaFailed);

            function userMediaSucceeded(stream) {
                // adding a local stream doesn't trigger the 'onaddstream' callback
                self._pc.addStream(stream);
                self.emit('localStreamAdded', stream);

                self._createLocalSDP('offer', offerOptions,
                // success
                function (sdp) {
                    DEBUG('Local SDP: %s', sdp);
                    self._sendCall(uri, sdp);
                },
                // failure
                function (error) {
                    DEBUG('Error creating local SDP: %s', error);
                    self._localTerminate(error);
                });
            }

            function userMediaFailed(error) {
                DEBUG('Error getting user media: %s', error);
                self._localTerminate(error);
            }
        }
    }, {
        key: '_initIncoming',
        value: function _initIncoming(id, caller, sdp) {
            this._id = id;
            this._remoteIdentity = caller;
            this._incomingSdp = sdp;
            this._direction = 'incoming';
            this._state = 'incoming';
        }
    }, {
        key: '_handleEvent',
        value: function _handleEvent(message) {
            var _this = this;

            DEBUG('Call event: %o', message);
            switch (message.event) {
                case 'state':
                    var oldState = this._state;
                    var newState = message.data.state;
                    this._state = newState;
                    var data = {};

                    if (newState === 'accepted' && this._direction === 'outgoing') {
                        (function () {
                            var self = _this;
                            _this._pc.setRemoteDescription(new _rtcninja2['default'].RTCSessionDescription({ type: 'answer', sdp: message.data.sdp }),
                            // success
                            function () {
                                DEBUG('Call accepted');
                                self.emit('stateChanged', oldState, newState, data);
                            },
                            // failure
                            function (error) {
                                DEBUG('Error accepting call: %s', error);
                                self.terminate();
                            });
                        })();
                    } else {
                        if (newState === 'terminated') {
                            data.reason = message.data.reason;
                            this._terminated = true;
                        }
                        this.emit('stateChanged', oldState, newState, data);
                        if (newState === 'terminated') {
                            this._closeRTCPeerConnection();
                        }
                    }
                    break;
                default:
                    break;
            }
        }
    }, {
        key: '_initRTCPeerConnection',
        value: function _initRTCPeerConnection(pcConfig) {
            if (this._pc !== null) {
                throw new Error('RTCPeerConnection already initialized');
            }

            var self = this;
            this._pc = new _rtcninja2['default'].RTCPeerConnection(pcConfig);
            this._pc.onaddstream = function (event, stream) {
                DEBUG('Stream added');
                self.emit('streamAdded', stream);
            };
            this._pc.onicecandidate = function (event) {
                var candidate = null;
                if (event.candidate !== null) {
                    candidate = {
                        'candidate': event.candidate.candidate,
                        'sdpMid': event.candidate.sdpMid,
                        'sdpMLineIndex': event.candidate.sdpMLineIndex
                    };
                    DEBUG('New ICE candidate %o', candidate);
                }
                self._sendTrickle(candidate);
            };
        }
    }, {
        key: '_createLocalSDP',
        value: function _createLocalSDP(type, options, onSuccess, onFailure) {
            var self = this;

            if (type === 'offer') {
                this._pc.createOffer(
                // success
                createSucceeded,
                // failure
                failure,
                // options
                options);
            } else if (type === 'answer') {
                this._pc.createAnswer(
                // success
                createSucceeded,
                // failure
                failure,
                // options
                options);
            } else {
                throw new Error('type must be "offer" or "answer", but "' + type + '" was given');
            }

            function createSucceeded(desc) {
                self._pc.setLocalDescription(desc,
                // success
                function () {
                    onSuccess(self._pc.localDescription.sdp);
                },
                // failure
                failure);
            }

            function failure(error) {
                onFailure(error);
            }
        }
    }, {
        key: '_sendRequest',
        value: function _sendRequest(req, cb) {
            this._account._sendRequest(req, cb);
        }
    }, {
        key: '_sendCall',
        value: function _sendCall(uri, sdp) {
            var req = {
                sylkrtc: 'session-create',
                account: this.account.id,
                session: this.id,
                uri: uri,
                sdp: sdp
            };
            var self = this;
            this._sendRequest(req, function (error) {
                if (error) {
                    DEBUG('Call error: %s', error);
                    self._localTerminate(error);
                }
            });
        }
    }, {
        key: '_sendTerminate',
        value: function _sendTerminate() {
            var req = {
                sylkrtc: 'session-terminate',
                session: this.id
            };
            var self = this;
            this._sendRequest(req, function (error) {
                if (error) {
                    DEBUG('Error terminating call: %s', error);
                    self._localTerminate(error);
                }
                self._terminated = true;
            });
        }
    }, {
        key: '_sendTrickle',
        value: function _sendTrickle(candidate) {
            var req = {
                sylkrtc: 'session-trickle',
                session: this.id,
                candidates: candidate !== null ? [candidate] : []
            };
            this._sendRequest(req, null);
        }
    }, {
        key: '_sendAnswer',
        value: function _sendAnswer(sdp) {
            var req = {
                sylkrtc: 'session-answer',
                session: this.id,
                sdp: sdp
            };
            var self = this;
            this._sendRequest(req, function (error) {
                if (error) {
                    DEBUG('Answer error: %s', error);
                    self.terminate();
                }
            });
        }
    }, {
        key: '_closeRTCPeerConnection',
        value: function _closeRTCPeerConnection() {
            DEBUG('Closing RTCPeerConnection');
            if (this._pc !== null) {
                var _iteratorNormalCompletion = true;
                var _didIteratorError = false;
                var _iteratorError = undefined;

                try {
                    for (var _iterator = this._pc.getLocalStreams()[Symbol.iterator](), _step; !(_iteratorNormalCompletion = (_step = _iterator.next()).done); _iteratorNormalCompletion = true) {
                        var stream = _step.value;

                        _rtcninja2['default'].closeMediaStream(stream);
                    }
                } catch (err) {
                    _didIteratorError = true;
                    _iteratorError = err;
                } finally {
                    try {
                        if (!_iteratorNormalCompletion && _iterator['return']) {
                            _iterator['return']();
                        }
                    } finally {
                        if (_didIteratorError) {
                            throw _iteratorError;
                        }
                    }
                }

                var _iteratorNormalCompletion2 = true;
                var _didIteratorError2 = false;
                var _iteratorError2 = undefined;

                try {
                    for (var _iterator2 = this._pc.getRemoteStreams()[Symbol.iterator](), _step2; !(_iteratorNormalCompletion2 = (_step2 = _iterator2.next()).done); _iteratorNormalCompletion2 = true) {
                        var stream = _step2.value;

                        _rtcninja2['default'].closeMediaStream(stream);
                    }
                } catch (err) {
                    _didIteratorError2 = true;
                    _iteratorError2 = err;
                } finally {
                    try {
                        if (!_iteratorNormalCompletion2 && _iterator2['return']) {
                            _iterator2['return']();
                        }
                    } finally {
                        if (_didIteratorError2) {
                            throw _iteratorError2;
                        }
                    }
                }

                this._pc.close();
                this._pc = null;
            }
        }
    }, {
        key: '_localTerminate',
        value: function _localTerminate(error) {
            if (this._terminated) {
                return;
            }
            this._account._calls['delete'](this.id);
            this._terminated = true;
            var oldState = this._state;
            var newState = 'terminated';
            var data = {
                reason: error.toString()
            };
            this.emit('stateChanged', oldState, newState, data);
            this._closeRTCPeerConnection();
        }
    }, {
        key: 'account',
        get: function get() {
            return this._account;
        }
    }, {
        key: 'id',
        get: function get() {
            return this._id;
        }
    }, {
        key: 'direction',
        get: function get() {
            return this._direction;
        }
    }, {
        key: 'state',
        get: function get() {
            return this._state;
        }
    }, {
        key: 'localIdentity',
        get: function get() {
            return this._account.id;
        }
    }, {
        key: 'remoteIdentity',
        get: function get() {
            return this._remoteIdentity;
        }
    }]);

    return Call;
})(_events.EventEmitter);

exports.Call = Call;

},{"debug":9,"events":6,"node-uuid":12,"rtcninja":15}],3:[function(require,module,exports){
(function (process){
'use strict';

Object.defineProperty(exports, '__esModule', {
    value: true
});

var _createClass = (function () {
    function defineProperties(target, props) {
        for (var i = 0; i < props.length; i++) {
            var descriptor = props[i];descriptor.enumerable = descriptor.enumerable || false;descriptor.configurable = true;if ('value' in descriptor) descriptor.writable = true;Object.defineProperty(target, descriptor.key, descriptor);
        }
    }return function (Constructor, protoProps, staticProps) {
        if (protoProps) defineProperties(Constructor.prototype, protoProps);if (staticProps) defineProperties(Constructor, staticProps);return Constructor;
    };
})();

var _get = function get(_x5, _x6, _x7) {
    var _again = true;_function: while (_again) {
        var object = _x5,
            property = _x6,
            receiver = _x7;desc = parent = getter = undefined;_again = false;if (object === null) object = Function.prototype;var desc = Object.getOwnPropertyDescriptor(object, property);if (desc === undefined) {
            var parent = Object.getPrototypeOf(object);if (parent === null) {
                return undefined;
            } else {
                _x5 = parent;_x6 = property;_x7 = receiver;_again = true;continue _function;
            }
        } else if ('value' in desc) {
            return desc.value;
        } else {
            var getter = desc.get;if (getter === undefined) {
                return undefined;
            }return getter.call(receiver);
        }
    }
};

function _interopRequireDefault(obj) {
    return obj && obj.__esModule ? obj : { 'default': obj };
}

function _classCallCheck(instance, Constructor) {
    if (!(instance instanceof Constructor)) {
        throw new TypeError('Cannot call a class as a function');
    }
}

function _inherits(subClass, superClass) {
    if (typeof superClass !== 'function' && superClass !== null) {
        throw new TypeError('Super expression must either be null or a function, not ' + typeof superClass);
    }subClass.prototype = Object.create(superClass && superClass.prototype, { constructor: { value: subClass, enumerable: false, writable: true, configurable: true } });if (superClass) subClass.__proto__ = superClass;
}

var _debug = require('debug');

var _debug2 = _interopRequireDefault(_debug);

var _nodeUuid = require('node-uuid');

var _nodeUuid2 = _interopRequireDefault(_nodeUuid);

var _events = require('events');

var _timers = require('timers');

var _websocket = require('websocket');

var _account = require('./account');

var SYLKRTC_PROTO = 'sylkRTC-1';
var DEBUG = (0, _debug2['default'])('sylkrtc:Connection');
var INITIAL_DELAY = 0.5 * 1000;

var Connection = (function (_EventEmitter) {
    _inherits(Connection, _EventEmitter);

    function Connection() {
        var options = arguments.length <= 0 || arguments[0] === undefined ? {} : arguments[0];

        _classCallCheck(this, Connection);

        if (!options.server) {
            throw new Error('"server" must be specified');
        }
        _get(Object.getPrototypeOf(Connection.prototype), 'constructor', this).call(this);
        this._wsUri = options.server;
        this._sock = null;
        this._state = null;
        this._closed = false;
        this._timer = null;
        this._delay = INITIAL_DELAY;
        this._accounts = new Map();
        this._requests = new Map();
    }

    _createClass(Connection, [{
        key: 'close',
        value: function close() {
            var _this = this;

            if (this._closed) {
                return;
            }
            this._closed = true;
            if (this._timer) {
                clearTimeout(this._timer);
                this._timer = null;
            }
            if (this._sock) {
                this._sock.close();
                this._sock = null;
            } else {
                (0, _timers.setImmediate)(function () {
                    _this._setState('closed');
                });
            }
        }
    }, {
        key: 'addAccount',
        value: function addAccount() {
            var _this2 = this;

            var options = arguments.length <= 0 || arguments[0] === undefined ? {} : arguments[0];
            var cb = arguments.length <= 1 || arguments[1] === undefined ? null : arguments[1];

            if (typeof options.account !== 'string' || typeof options.password !== 'string') {
                throw new Error('Invalid options, "account" and "password" must be supplied');
            }
            if (this._accounts.has(options.account)) {
                throw new Error('Account already added');
            }

            var acc = new _account.Account(options.account, options.password, this);
            // add it early to the set so we don't add it more than once, ever
            this._accounts.set(acc.id, acc);

            var req = {
                sylkrtc: 'account-add',
                account: acc.id,
                password: acc.password
            };
            this._sendRequest(req, function (error) {
                if (error) {
                    DEBUG('add_account error: %s', error);
                    _this2._accounts['delete'](acc.id);
                    acc = null;
                }
                if (cb) {
                    cb(error, acc);
                }
            });
        }
    }, {
        key: 'removeAccount',
        value: function removeAccount(account) {
            var cb = arguments.length <= 1 || arguments[1] === undefined ? null : arguments[1];

            var acc = this._accounts.get(account.id);
            if (account !== acc) {
                throw new Error('Unknown account');
            }

            // delete the account from the mapping, regardless of the result
            this._accounts['delete'](account.id);

            var req = {
                sylkrtc: 'account-remove',
                account: acc.id
            };
            this._sendRequest(req, function (error) {
                if (error) {
                    DEBUG('remove_account error: %s', error);
                }
                if (cb) {
                    cb();
                }
            });
        }
    }, {
        key: '_initialize',

        // Private API

        value: function _initialize() {
            var _this3 = this;

            if (this._sock !== null) {
                throw new Error('WebSocket already initialized');
            }
            if (this._timer !== null) {
                throw new Error('Initialize is in progress');
            }

            DEBUG('Initializing');

            if (process.browser) {
                window.addEventListener('beforeunload', function () {
                    if (_this3._sock !== null) {
                        var noop = function noop() {};
                        _this3._sock.onerror = noop;
                        _this3._sock.onmessage = noop;
                        _this3._sock.onclose = noop;
                        _this3._sock.close();
                    }
                });
            }

            this._timer = setTimeout(function () {
                _this3._connect();
            }, this._delay);
        }
    }, {
        key: '_connect',
        value: function _connect() {
            var _this4 = this;

            DEBUG('WebSocket connecting');
            this._setState('connecting');

            this._sock = new _websocket.w3cwebsocket(this._wsUri, SYLKRTC_PROTO);
            this._sock.onopen = function () {
                DEBUG('WebSocket connection open');
                _this4._onOpen();
            };
            this._sock.onerror = function () {
                DEBUG('WebSocket connection got error');
            };
            this._sock.onclose = function (event) {
                DEBUG('WebSocket connection closed: %d: (reason="%s", clean=%s)', event.code, event.reason, event.wasClean);
                _this4._onClose();
            };
            this._sock.onmessage = function (event) {
                DEBUG('WebSocket received message: %o', event);
                _this4._onMessage(event);
            };
        }
    }, {
        key: '_sendRequest',
        value: function _sendRequest(req, cb) {
            var transaction = _nodeUuid2['default'].v4();
            req.transaction = transaction;
            if (this._state !== 'ready') {
                (0, _timers.setImmediate)(function () {
                    cb(new Error('Connection is not ready'));
                });
                return;
            }
            this._requests.set(transaction, { req: req, cb: cb });
            this._sock.send(JSON.stringify(req));
        }
    }, {
        key: '_setState',
        value: function _setState(newState) {
            DEBUG('Set state: %s -> %s', this._state, newState);
            var oldState = this._state;
            this._state = newState;
            this.emit('stateChanged', oldState, newState);
        }
    }, {
        key: '_onOpen',

        // WebSocket callbacks

        value: function _onOpen() {
            clearTimeout(this._timer);
            this._timer = null;
            this._delay = INITIAL_DELAY;
            this._setState('connected');
        }
    }, {
        key: '_onClose',
        value: function _onClose() {
            var _this5 = this;

            this._sock = null;
            if (this._timer) {
                clearTimeout(this._timer);
                this._timer = null;
            }

            // remove all accounts, the server no longer has them anyway
            this._accounts.clear();

            this._setState('disconnected');
            if (!this._closed) {
                this._delay = this._delay * 2;
                if (this._delay > Number.MAX_VALUE) {
                    this._delay = INITIAL_DELAY;
                }
                DEBUG('Retrying connection in %s seconds', this._delay / 1000);
                this._timer = setTimeout(function () {
                    _this5._connect();
                }, this._delay);
            } else {
                this._setState('closed');
            }
        }
    }, {
        key: '_onMessage',
        value: function _onMessage(event) {
            var message = JSON.parse(event.data);
            if (typeof message.sylkrtc === 'undefined') {
                DEBUG('Unrecognized message received');
                return;
            }

            DEBUG('Received "%s" message: %o', message.sylkrtc, message);

            if (message.sylkrtc === 'event') {
                DEBUG('Received event: "%s"', message.event);
                switch (message.event) {
                    case 'ready':
                        this._setState('ready');
                        break;
                    default:
                        break;
                }
            } else if (message.sylkrtc === 'account_event') {
                var acc = this._accounts.get(message.account);
                if (!acc) {
                    DEBUG('Account %s not found', message.account);
                    return;
                }
                acc._handleEvent(message);
            } else if (message.sylkrtc === 'session_event') {
                var sessionId = message.session;
                var _iteratorNormalCompletion = true;
                var _didIteratorError = false;
                var _iteratorError = undefined;

                try {
                    for (var _iterator = this._accounts.values()[Symbol.iterator](), _step; !(_iteratorNormalCompletion = (_step = _iterator.next()).done); _iteratorNormalCompletion = true) {
                        var acc = _step.value;

                        var call = acc._calls.get(sessionId);
                        if (call) {
                            call._handleEvent(message);
                            break;
                        }
                    }
                } catch (err) {
                    _didIteratorError = true;
                    _iteratorError = err;
                } finally {
                    try {
                        if (!_iteratorNormalCompletion && _iterator['return']) {
                            _iterator['return']();
                        }
                    } finally {
                        if (_didIteratorError) {
                            throw _iteratorError;
                        }
                    }
                }
            } else if (message.sylkrtc === 'ack' || message.sylkrtc === 'error') {
                var transaction = message.transaction;
                var data = this._requests.get(transaction);
                if (!data) {
                    DEBUG('Could not find transaction %s', transaction);
                    return;
                }
                this._requests['delete'](transaction);
                DEBUG('Received "%s" for request: %o', message.sylkrtc, data.req);
                if (data.cb) {
                    if (message.sylkrtc === 'ack') {
                        data.cb(null);
                    } else {
                        data.cb(new Error(message.error));
                    }
                }
            }
        }
    }, {
        key: 'state',
        get: function get() {
            return this._state;
        }
    }]);

    return Connection;
})(_events.EventEmitter);

exports.Connection = Connection;

}).call(this,require('_process'))

},{"./account":1,"_process":7,"debug":9,"events":6,"node-uuid":12,"timers":8,"websocket":20}],4:[function(require,module,exports){
'use strict';

Object.defineProperty(exports, '__esModule', {
    value: true
});

function _interopRequireDefault(obj) {
    return obj && obj.__esModule ? obj : { 'default': obj };
}

var _debug = require('debug');

var _debug2 = _interopRequireDefault(_debug);

var _rtcninja = require('rtcninja');

var _rtcninja2 = _interopRequireDefault(_rtcninja);

var _connection = require('./connection');

// Public API

function createConnection() {
    var options = arguments.length <= 0 || arguments[0] === undefined ? {} : arguments[0];

    if (!_rtcninja2['default'].hasWebRTC()) {
        throw new Error('WebRTC support not detected');
    }

    var conn = new _connection.Connection(options);
    conn._initialize();
    return conn;
}

// Some proxied functions from rtcninja

function isWebRTCSupported() {
    return _rtcninja2['default'].hasWebRTC();
}

function attachMediaStream(element, stream) {
    return _rtcninja2['default'].attachMediaStream(element, stream);
}

function closeMediaStream(stream) {
    _rtcninja2['default'].closeMediaStream(stream);
}

exports['default'] = {
    createConnection: createConnection,
    debug: _debug2['default'],
    attachMediaStream: attachMediaStream, closeMediaStream: closeMediaStream, isWebRTCSupported: isWebRTCSupported
};
module.exports = exports['default'];

},{"./connection":3,"debug":9,"rtcninja":15}],5:[function(require,module,exports){
/*
 * JavaScript MD5 1.0.1
 * https://github.com/blueimp/JavaScript-MD5
 *
 * Copyright 2011, Sebastian Tschan
 * https://blueimp.net
 *
 * Licensed under the MIT license:
 * http://www.opensource.org/licenses/MIT
 * 
 * Based on
 * A JavaScript implementation of the RSA Data Security, Inc. MD5 Message
 * Digest Algorithm, as defined in RFC 1321.
 * Version 2.2 Copyright (C) Paul Johnston 1999 - 2009
 * Other contributors: Greg Holt, Andrew Kepert, Ydnar, Lostinet
 * Distributed under the BSD License
 * See http://pajhome.org.uk/crypt/md5 for more info.
 */

/*jslint bitwise: true */
/*global unescape, define */

(function ($) {
    'use strict';

    /*
    * Add integers, wrapping at 2^32. This uses 16-bit operations internally
    * to work around bugs in some JS interpreters.
    */
    function safe_add(x, y) {
        var lsw = (x & 0xFFFF) + (y & 0xFFFF),
            msw = (x >> 16) + (y >> 16) + (lsw >> 16);
        return (msw << 16) | (lsw & 0xFFFF);
    }

    /*
    * Bitwise rotate a 32-bit number to the left.
    */
    function bit_rol(num, cnt) {
        return (num << cnt) | (num >>> (32 - cnt));
    }

    /*
    * These functions implement the four basic operations the algorithm uses.
    */
    function md5_cmn(q, a, b, x, s, t) {
        return safe_add(bit_rol(safe_add(safe_add(a, q), safe_add(x, t)), s), b);
    }
    function md5_ff(a, b, c, d, x, s, t) {
        return md5_cmn((b & c) | ((~b) & d), a, b, x, s, t);
    }
    function md5_gg(a, b, c, d, x, s, t) {
        return md5_cmn((b & d) | (c & (~d)), a, b, x, s, t);
    }
    function md5_hh(a, b, c, d, x, s, t) {
        return md5_cmn(b ^ c ^ d, a, b, x, s, t);
    }
    function md5_ii(a, b, c, d, x, s, t) {
        return md5_cmn(c ^ (b | (~d)), a, b, x, s, t);
    }

    /*
    * Calculate the MD5 of an array of little-endian words, and a bit length.
    */
    function binl_md5(x, len) {
        /* append padding */
        x[len >> 5] |= 0x80 << (len % 32);
        x[(((len + 64) >>> 9) << 4) + 14] = len;

        var i, olda, oldb, oldc, oldd,
            a =  1732584193,
            b = -271733879,
            c = -1732584194,
            d =  271733878;

        for (i = 0; i < x.length; i += 16) {
            olda = a;
            oldb = b;
            oldc = c;
            oldd = d;

            a = md5_ff(a, b, c, d, x[i],       7, -680876936);
            d = md5_ff(d, a, b, c, x[i +  1], 12, -389564586);
            c = md5_ff(c, d, a, b, x[i +  2], 17,  606105819);
            b = md5_ff(b, c, d, a, x[i +  3], 22, -1044525330);
            a = md5_ff(a, b, c, d, x[i +  4],  7, -176418897);
            d = md5_ff(d, a, b, c, x[i +  5], 12,  1200080426);
            c = md5_ff(c, d, a, b, x[i +  6], 17, -1473231341);
            b = md5_ff(b, c, d, a, x[i +  7], 22, -45705983);
            a = md5_ff(a, b, c, d, x[i +  8],  7,  1770035416);
            d = md5_ff(d, a, b, c, x[i +  9], 12, -1958414417);
            c = md5_ff(c, d, a, b, x[i + 10], 17, -42063);
            b = md5_ff(b, c, d, a, x[i + 11], 22, -1990404162);
            a = md5_ff(a, b, c, d, x[i + 12],  7,  1804603682);
            d = md5_ff(d, a, b, c, x[i + 13], 12, -40341101);
            c = md5_ff(c, d, a, b, x[i + 14], 17, -1502002290);
            b = md5_ff(b, c, d, a, x[i + 15], 22,  1236535329);

            a = md5_gg(a, b, c, d, x[i +  1],  5, -165796510);
            d = md5_gg(d, a, b, c, x[i +  6],  9, -1069501632);
            c = md5_gg(c, d, a, b, x[i + 11], 14,  643717713);
            b = md5_gg(b, c, d, a, x[i],      20, -373897302);
            a = md5_gg(a, b, c, d, x[i +  5],  5, -701558691);
            d = md5_gg(d, a, b, c, x[i + 10],  9,  38016083);
            c = md5_gg(c, d, a, b, x[i + 15], 14, -660478335);
            b = md5_gg(b, c, d, a, x[i +  4], 20, -405537848);
            a = md5_gg(a, b, c, d, x[i +  9],  5,  568446438);
            d = md5_gg(d, a, b, c, x[i + 14],  9, -1019803690);
            c = md5_gg(c, d, a, b, x[i +  3], 14, -187363961);
            b = md5_gg(b, c, d, a, x[i +  8], 20,  1163531501);
            a = md5_gg(a, b, c, d, x[i + 13],  5, -1444681467);
            d = md5_gg(d, a, b, c, x[i +  2],  9, -51403784);
            c = md5_gg(c, d, a, b, x[i +  7], 14,  1735328473);
            b = md5_gg(b, c, d, a, x[i + 12], 20, -1926607734);

            a = md5_hh(a, b, c, d, x[i +  5],  4, -378558);
            d = md5_hh(d, a, b, c, x[i +  8], 11, -2022574463);
            c = md5_hh(c, d, a, b, x[i + 11], 16,  1839030562);
            b = md5_hh(b, c, d, a, x[i + 14], 23, -35309556);
            a = md5_hh(a, b, c, d, x[i +  1],  4, -1530992060);
            d = md5_hh(d, a, b, c, x[i +  4], 11,  1272893353);
            c = md5_hh(c, d, a, b, x[i +  7], 16, -155497632);
            b = md5_hh(b, c, d, a, x[i + 10], 23, -1094730640);
            a = md5_hh(a, b, c, d, x[i + 13],  4,  681279174);
            d = md5_hh(d, a, b, c, x[i],      11, -358537222);
            c = md5_hh(c, d, a, b, x[i +  3], 16, -722521979);
            b = md5_hh(b, c, d, a, x[i +  6], 23,  76029189);
            a = md5_hh(a, b, c, d, x[i +  9],  4, -640364487);
            d = md5_hh(d, a, b, c, x[i + 12], 11, -421815835);
            c = md5_hh(c, d, a, b, x[i + 15], 16,  530742520);
            b = md5_hh(b, c, d, a, x[i +  2], 23, -995338651);

            a = md5_ii(a, b, c, d, x[i],       6, -198630844);
            d = md5_ii(d, a, b, c, x[i +  7], 10,  1126891415);
            c = md5_ii(c, d, a, b, x[i + 14], 15, -1416354905);
            b = md5_ii(b, c, d, a, x[i +  5], 21, -57434055);
            a = md5_ii(a, b, c, d, x[i + 12],  6,  1700485571);
            d = md5_ii(d, a, b, c, x[i +  3], 10, -1894986606);
            c = md5_ii(c, d, a, b, x[i + 10], 15, -1051523);
            b = md5_ii(b, c, d, a, x[i +  1], 21, -2054922799);
            a = md5_ii(a, b, c, d, x[i +  8],  6,  1873313359);
            d = md5_ii(d, a, b, c, x[i + 15], 10, -30611744);
            c = md5_ii(c, d, a, b, x[i +  6], 15, -1560198380);
            b = md5_ii(b, c, d, a, x[i + 13], 21,  1309151649);
            a = md5_ii(a, b, c, d, x[i +  4],  6, -145523070);
            d = md5_ii(d, a, b, c, x[i + 11], 10, -1120210379);
            c = md5_ii(c, d, a, b, x[i +  2], 15,  718787259);
            b = md5_ii(b, c, d, a, x[i +  9], 21, -343485551);

            a = safe_add(a, olda);
            b = safe_add(b, oldb);
            c = safe_add(c, oldc);
            d = safe_add(d, oldd);
        }
        return [a, b, c, d];
    }

    /*
    * Convert an array of little-endian words to a string
    */
    function binl2rstr(input) {
        var i,
            output = '';
        for (i = 0; i < input.length * 32; i += 8) {
            output += String.fromCharCode((input[i >> 5] >>> (i % 32)) & 0xFF);
        }
        return output;
    }

    /*
    * Convert a raw string to an array of little-endian words
    * Characters >255 have their high-byte silently ignored.
    */
    function rstr2binl(input) {
        var i,
            output = [];
        output[(input.length >> 2) - 1] = undefined;
        for (i = 0; i < output.length; i += 1) {
            output[i] = 0;
        }
        for (i = 0; i < input.length * 8; i += 8) {
            output[i >> 5] |= (input.charCodeAt(i / 8) & 0xFF) << (i % 32);
        }
        return output;
    }

    /*
    * Calculate the MD5 of a raw string
    */
    function rstr_md5(s) {
        return binl2rstr(binl_md5(rstr2binl(s), s.length * 8));
    }

    /*
    * Calculate the HMAC-MD5, of a key and some data (raw strings)
    */
    function rstr_hmac_md5(key, data) {
        var i,
            bkey = rstr2binl(key),
            ipad = [],
            opad = [],
            hash;
        ipad[15] = opad[15] = undefined;
        if (bkey.length > 16) {
            bkey = binl_md5(bkey, key.length * 8);
        }
        for (i = 0; i < 16; i += 1) {
            ipad[i] = bkey[i] ^ 0x36363636;
            opad[i] = bkey[i] ^ 0x5C5C5C5C;
        }
        hash = binl_md5(ipad.concat(rstr2binl(data)), 512 + data.length * 8);
        return binl2rstr(binl_md5(opad.concat(hash), 512 + 128));
    }

    /*
    * Convert a raw string to a hex string
    */
    function rstr2hex(input) {
        var hex_tab = '0123456789abcdef',
            output = '',
            x,
            i;
        for (i = 0; i < input.length; i += 1) {
            x = input.charCodeAt(i);
            output += hex_tab.charAt((x >>> 4) & 0x0F) +
                hex_tab.charAt(x & 0x0F);
        }
        return output;
    }

    /*
    * Encode a string as utf-8
    */
    function str2rstr_utf8(input) {
        return unescape(encodeURIComponent(input));
    }

    /*
    * Take string arguments and return either raw or hex encoded strings
    */
    function raw_md5(s) {
        return rstr_md5(str2rstr_utf8(s));
    }
    function hex_md5(s) {
        return rstr2hex(raw_md5(s));
    }
    function raw_hmac_md5(k, d) {
        return rstr_hmac_md5(str2rstr_utf8(k), str2rstr_utf8(d));
    }
    function hex_hmac_md5(k, d) {
        return rstr2hex(raw_hmac_md5(k, d));
    }

    function md5(string, key, raw) {
        if (!key) {
            if (!raw) {
                return hex_md5(string);
            }
            return raw_md5(string);
        }
        if (!raw) {
            return hex_hmac_md5(key, string);
        }
        return raw_hmac_md5(key, string);
    }

    if (typeof define === 'function' && define.amd) {
        define(function () {
            return md5;
        });
    } else {
        $.md5 = md5;
    }
}(this));

},{}],6:[function(require,module,exports){
// Copyright Joyent, Inc. and other Node contributors.
//
// Permission is hereby granted, free of charge, to any person obtaining a
// copy of this software and associated documentation files (the
// "Software"), to deal in the Software without restriction, including
// without limitation the rights to use, copy, modify, merge, publish,
// distribute, sublicense, and/or sell copies of the Software, and to permit
// persons to whom the Software is furnished to do so, subject to the
// following conditions:
//
// The above copyright notice and this permission notice shall be included
// in all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
// OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
// MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
// NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
// DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
// OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
// USE OR OTHER DEALINGS IN THE SOFTWARE.

function EventEmitter() {
  this._events = this._events || {};
  this._maxListeners = this._maxListeners || undefined;
}
module.exports = EventEmitter;

// Backwards-compat with node 0.10.x
EventEmitter.EventEmitter = EventEmitter;

EventEmitter.prototype._events = undefined;
EventEmitter.prototype._maxListeners = undefined;

// By default EventEmitters will print a warning if more than 10 listeners are
// added to it. This is a useful default which helps finding memory leaks.
EventEmitter.defaultMaxListeners = 10;

// Obviously not all Emitters should be limited to 10. This function allows
// that to be increased. Set to zero for unlimited.
EventEmitter.prototype.setMaxListeners = function(n) {
  if (!isNumber(n) || n < 0 || isNaN(n))
    throw TypeError('n must be a positive number');
  this._maxListeners = n;
  return this;
};

EventEmitter.prototype.emit = function(type) {
  var er, handler, len, args, i, listeners;

  if (!this._events)
    this._events = {};

  // If there is no 'error' event listener then throw.
  if (type === 'error') {
    if (!this._events.error ||
        (isObject(this._events.error) && !this._events.error.length)) {
      er = arguments[1];
      if (er instanceof Error) {
        throw er; // Unhandled 'error' event
      }
      throw TypeError('Uncaught, unspecified "error" event.');
    }
  }

  handler = this._events[type];

  if (isUndefined(handler))
    return false;

  if (isFunction(handler)) {
    switch (arguments.length) {
      // fast cases
      case 1:
        handler.call(this);
        break;
      case 2:
        handler.call(this, arguments[1]);
        break;
      case 3:
        handler.call(this, arguments[1], arguments[2]);
        break;
      // slower
      default:
        len = arguments.length;
        args = new Array(len - 1);
        for (i = 1; i < len; i++)
          args[i - 1] = arguments[i];
        handler.apply(this, args);
    }
  } else if (isObject(handler)) {
    len = arguments.length;
    args = new Array(len - 1);
    for (i = 1; i < len; i++)
      args[i - 1] = arguments[i];

    listeners = handler.slice();
    len = listeners.length;
    for (i = 0; i < len; i++)
      listeners[i].apply(this, args);
  }

  return true;
};

EventEmitter.prototype.addListener = function(type, listener) {
  var m;

  if (!isFunction(listener))
    throw TypeError('listener must be a function');

  if (!this._events)
    this._events = {};

  // To avoid recursion in the case that type === "newListener"! Before
  // adding it to the listeners, first emit "newListener".
  if (this._events.newListener)
    this.emit('newListener', type,
              isFunction(listener.listener) ?
              listener.listener : listener);

  if (!this._events[type])
    // Optimize the case of one listener. Don't need the extra array object.
    this._events[type] = listener;
  else if (isObject(this._events[type]))
    // If we've already got an array, just append.
    this._events[type].push(listener);
  else
    // Adding the second element, need to change to array.
    this._events[type] = [this._events[type], listener];

  // Check for listener leak
  if (isObject(this._events[type]) && !this._events[type].warned) {
    var m;
    if (!isUndefined(this._maxListeners)) {
      m = this._maxListeners;
    } else {
      m = EventEmitter.defaultMaxListeners;
    }

    if (m && m > 0 && this._events[type].length > m) {
      this._events[type].warned = true;
      console.error('(node) warning: possible EventEmitter memory ' +
                    'leak detected. %d listeners added. ' +
                    'Use emitter.setMaxListeners() to increase limit.',
                    this._events[type].length);
      if (typeof console.trace === 'function') {
        // not supported in IE 10
        console.trace();
      }
    }
  }

  return this;
};

EventEmitter.prototype.on = EventEmitter.prototype.addListener;

EventEmitter.prototype.once = function(type, listener) {
  if (!isFunction(listener))
    throw TypeError('listener must be a function');

  var fired = false;

  function g() {
    this.removeListener(type, g);

    if (!fired) {
      fired = true;
      listener.apply(this, arguments);
    }
  }

  g.listener = listener;
  this.on(type, g);

  return this;
};

// emits a 'removeListener' event iff the listener was removed
EventEmitter.prototype.removeListener = function(type, listener) {
  var list, position, length, i;

  if (!isFunction(listener))
    throw TypeError('listener must be a function');

  if (!this._events || !this._events[type])
    return this;

  list = this._events[type];
  length = list.length;
  position = -1;

  if (list === listener ||
      (isFunction(list.listener) && list.listener === listener)) {
    delete this._events[type];
    if (this._events.removeListener)
      this.emit('removeListener', type, listener);

  } else if (isObject(list)) {
    for (i = length; i-- > 0;) {
      if (list[i] === listener ||
          (list[i].listener && list[i].listener === listener)) {
        position = i;
        break;
      }
    }

    if (position < 0)
      return this;

    if (list.length === 1) {
      list.length = 0;
      delete this._events[type];
    } else {
      list.splice(position, 1);
    }

    if (this._events.removeListener)
      this.emit('removeListener', type, listener);
  }

  return this;
};

EventEmitter.prototype.removeAllListeners = function(type) {
  var key, listeners;

  if (!this._events)
    return this;

  // not listening for removeListener, no need to emit
  if (!this._events.removeListener) {
    if (arguments.length === 0)
      this._events = {};
    else if (this._events[type])
      delete this._events[type];
    return this;
  }

  // emit removeListener for all listeners on all events
  if (arguments.length === 0) {
    for (key in this._events) {
      if (key === 'removeListener') continue;
      this.removeAllListeners(key);
    }
    this.removeAllListeners('removeListener');
    this._events = {};
    return this;
  }

  listeners = this._events[type];

  if (isFunction(listeners)) {
    this.removeListener(type, listeners);
  } else {
    // LIFO order
    while (listeners.length)
      this.removeListener(type, listeners[listeners.length - 1]);
  }
  delete this._events[type];

  return this;
};

EventEmitter.prototype.listeners = function(type) {
  var ret;
  if (!this._events || !this._events[type])
    ret = [];
  else if (isFunction(this._events[type]))
    ret = [this._events[type]];
  else
    ret = this._events[type].slice();
  return ret;
};

EventEmitter.listenerCount = function(emitter, type) {
  var ret;
  if (!emitter._events || !emitter._events[type])
    ret = 0;
  else if (isFunction(emitter._events[type]))
    ret = 1;
  else
    ret = emitter._events[type].length;
  return ret;
};

function isFunction(arg) {
  return typeof arg === 'function';
}

function isNumber(arg) {
  return typeof arg === 'number';
}

function isObject(arg) {
  return typeof arg === 'object' && arg !== null;
}

function isUndefined(arg) {
  return arg === void 0;
}

},{}],7:[function(require,module,exports){
// shim for using process in browser

var process = module.exports = {};
var queue = [];
var draining = false;
var currentQueue;
var queueIndex = -1;

function cleanUpNextTick() {
    draining = false;
    if (currentQueue.length) {
        queue = currentQueue.concat(queue);
    } else {
        queueIndex = -1;
    }
    if (queue.length) {
        drainQueue();
    }
}

function drainQueue() {
    if (draining) {
        return;
    }
    var timeout = setTimeout(cleanUpNextTick);
    draining = true;

    var len = queue.length;
    while(len) {
        currentQueue = queue;
        queue = [];
        while (++queueIndex < len) {
            currentQueue[queueIndex].run();
        }
        queueIndex = -1;
        len = queue.length;
    }
    currentQueue = null;
    draining = false;
    clearTimeout(timeout);
}

process.nextTick = function (fun) {
    var args = new Array(arguments.length - 1);
    if (arguments.length > 1) {
        for (var i = 1; i < arguments.length; i++) {
            args[i - 1] = arguments[i];
        }
    }
    queue.push(new Item(fun, args));
    if (queue.length === 1 && !draining) {
        setTimeout(drainQueue, 0);
    }
};

// v8 likes predictible objects
function Item(fun, array) {
    this.fun = fun;
    this.array = array;
}
Item.prototype.run = function () {
    this.fun.apply(null, this.array);
};
process.title = 'browser';
process.browser = true;
process.env = {};
process.argv = [];
process.version = ''; // empty string to avoid regexp issues
process.versions = {};

function noop() {}

process.on = noop;
process.addListener = noop;
process.once = noop;
process.off = noop;
process.removeListener = noop;
process.removeAllListeners = noop;
process.emit = noop;

process.binding = function (name) {
    throw new Error('process.binding is not supported');
};

// TODO(shtylman)
process.cwd = function () { return '/' };
process.chdir = function (dir) {
    throw new Error('process.chdir is not supported');
};
process.umask = function() { return 0; };

},{}],8:[function(require,module,exports){
var nextTick = require('process/browser.js').nextTick;
var apply = Function.prototype.apply;
var slice = Array.prototype.slice;
var immediateIds = {};
var nextImmediateId = 0;

// DOM APIs, for completeness

exports.setTimeout = function() {
  return new Timeout(apply.call(setTimeout, window, arguments), clearTimeout);
};
exports.setInterval = function() {
  return new Timeout(apply.call(setInterval, window, arguments), clearInterval);
};
exports.clearTimeout =
exports.clearInterval = function(timeout) { timeout.close(); };

function Timeout(id, clearFn) {
  this._id = id;
  this._clearFn = clearFn;
}
Timeout.prototype.unref = Timeout.prototype.ref = function() {};
Timeout.prototype.close = function() {
  this._clearFn.call(window, this._id);
};

// Does not start the time, just sets up the members needed.
exports.enroll = function(item, msecs) {
  clearTimeout(item._idleTimeoutId);
  item._idleTimeout = msecs;
};

exports.unenroll = function(item) {
  clearTimeout(item._idleTimeoutId);
  item._idleTimeout = -1;
};

exports._unrefActive = exports.active = function(item) {
  clearTimeout(item._idleTimeoutId);

  var msecs = item._idleTimeout;
  if (msecs >= 0) {
    item._idleTimeoutId = setTimeout(function onTimeout() {
      if (item._onTimeout)
        item._onTimeout();
    }, msecs);
  }
};

// That's not how node.js implements it but the exposed api is the same.
exports.setImmediate = typeof setImmediate === "function" ? setImmediate : function(fn) {
  var id = nextImmediateId++;
  var args = arguments.length < 2 ? false : slice.call(arguments, 1);

  immediateIds[id] = true;

  nextTick(function onNextTick() {
    if (immediateIds[id]) {
      // fn.call() is faster so we optimize for the common use-case
      // @see http://jsperf.com/call-apply-segu
      if (args) {
        fn.apply(null, args);
      } else {
        fn.call(null);
      }
      // Prevent ids from leaking
      exports.clearImmediate(id);
    }
  });

  return id;
};

exports.clearImmediate = typeof clearImmediate === "function" ? clearImmediate : function(id) {
  delete immediateIds[id];
};
},{"process/browser.js":7}],9:[function(require,module,exports){

/**
 * This is the web browser implementation of `debug()`.
 *
 * Expose `debug()` as the module.
 */

exports = module.exports = require('./debug');
exports.log = log;
exports.formatArgs = formatArgs;
exports.save = save;
exports.load = load;
exports.useColors = useColors;
exports.storage = 'undefined' != typeof chrome
               && 'undefined' != typeof chrome.storage
                  ? chrome.storage.local
                  : localstorage();

/**
 * Colors.
 */

exports.colors = [
  'lightseagreen',
  'forestgreen',
  'goldenrod',
  'dodgerblue',
  'darkorchid',
  'crimson'
];

/**
 * Currently only WebKit-based Web Inspectors, Firefox >= v31,
 * and the Firebug extension (any Firefox version) are known
 * to support "%c" CSS customizations.
 *
 * TODO: add a `localStorage` variable to explicitly enable/disable colors
 */

function useColors() {
  // is webkit? http://stackoverflow.com/a/16459606/376773
  return ('WebkitAppearance' in document.documentElement.style) ||
    // is firebug? http://stackoverflow.com/a/398120/376773
    (window.console && (console.firebug || (console.exception && console.table))) ||
    // is firefox >= v31?
    // https://developer.mozilla.org/en-US/docs/Tools/Web_Console#Styling_messages
    (navigator.userAgent.toLowerCase().match(/firefox\/(\d+)/) && parseInt(RegExp.$1, 10) >= 31);
}

/**
 * Map %j to `JSON.stringify()`, since no Web Inspectors do that by default.
 */

exports.formatters.j = function(v) {
  return JSON.stringify(v);
};


/**
 * Colorize log arguments if enabled.
 *
 * @api public
 */

function formatArgs() {
  var args = arguments;
  var useColors = this.useColors;

  args[0] = (useColors ? '%c' : '')
    + this.namespace
    + (useColors ? ' %c' : ' ')
    + args[0]
    + (useColors ? '%c ' : ' ')
    + '+' + exports.humanize(this.diff);

  if (!useColors) return args;

  var c = 'color: ' + this.color;
  args = [args[0], c, 'color: inherit'].concat(Array.prototype.slice.call(args, 1));

  // the final "%c" is somewhat tricky, because there could be other
  // arguments passed either before or after the %c, so we need to
  // figure out the correct index to insert the CSS into
  var index = 0;
  var lastC = 0;
  args[0].replace(/%[a-z%]/g, function(match) {
    if ('%%' === match) return;
    index++;
    if ('%c' === match) {
      // we only are interested in the *last* %c
      // (the user may have provided their own)
      lastC = index;
    }
  });

  args.splice(lastC, 0, c);
  return args;
}

/**
 * Invokes `console.log()` when available.
 * No-op when `console.log` is not a "function".
 *
 * @api public
 */

function log() {
  // this hackery is required for IE8/9, where
  // the `console.log` function doesn't have 'apply'
  return 'object' === typeof console
    && console.log
    && Function.prototype.apply.call(console.log, console, arguments);
}

/**
 * Save `namespaces`.
 *
 * @param {String} namespaces
 * @api private
 */

function save(namespaces) {
  try {
    if (null == namespaces) {
      exports.storage.removeItem('debug');
    } else {
      exports.storage.debug = namespaces;
    }
  } catch(e) {}
}

/**
 * Load `namespaces`.
 *
 * @return {String} returns the previously persisted debug modes
 * @api private
 */

function load() {
  var r;
  try {
    r = exports.storage.debug;
  } catch(e) {}
  return r;
}

/**
 * Enable namespaces listed in `localStorage.debug` initially.
 */

exports.enable(load());

/**
 * Localstorage attempts to return the localstorage.
 *
 * This is necessary because safari throws
 * when a user disables cookies/localstorage
 * and you attempt to access it.
 *
 * @return {LocalStorage}
 * @api private
 */

function localstorage(){
  try {
    return window.localStorage;
  } catch (e) {}
}

},{"./debug":10}],10:[function(require,module,exports){

/**
 * This is the common logic for both the Node.js and web browser
 * implementations of `debug()`.
 *
 * Expose `debug()` as the module.
 */

exports = module.exports = debug;
exports.coerce = coerce;
exports.disable = disable;
exports.enable = enable;
exports.enabled = enabled;
exports.humanize = require('ms');

/**
 * The currently active debug mode names, and names to skip.
 */

exports.names = [];
exports.skips = [];

/**
 * Map of special "%n" handling functions, for the debug "format" argument.
 *
 * Valid key names are a single, lowercased letter, i.e. "n".
 */

exports.formatters = {};

/**
 * Previously assigned color.
 */

var prevColor = 0;

/**
 * Previous log timestamp.
 */

var prevTime;

/**
 * Select a color.
 *
 * @return {Number}
 * @api private
 */

function selectColor() {
  return exports.colors[prevColor++ % exports.colors.length];
}

/**
 * Create a debugger with the given `namespace`.
 *
 * @param {String} namespace
 * @return {Function}
 * @api public
 */

function debug(namespace) {

  // define the `disabled` version
  function disabled() {
  }
  disabled.enabled = false;

  // define the `enabled` version
  function enabled() {

    var self = enabled;

    // set `diff` timestamp
    var curr = +new Date();
    var ms = curr - (prevTime || curr);
    self.diff = ms;
    self.prev = prevTime;
    self.curr = curr;
    prevTime = curr;

    // add the `color` if not set
    if (null == self.useColors) self.useColors = exports.useColors();
    if (null == self.color && self.useColors) self.color = selectColor();

    var args = Array.prototype.slice.call(arguments);

    args[0] = exports.coerce(args[0]);

    if ('string' !== typeof args[0]) {
      // anything else let's inspect with %o
      args = ['%o'].concat(args);
    }

    // apply any `formatters` transformations
    var index = 0;
    args[0] = args[0].replace(/%([a-z%])/g, function(match, format) {
      // if we encounter an escaped % then don't increase the array index
      if (match === '%%') return match;
      index++;
      var formatter = exports.formatters[format];
      if ('function' === typeof formatter) {
        var val = args[index];
        match = formatter.call(self, val);

        // now we need to remove `args[index]` since it's inlined in the `format`
        args.splice(index, 1);
        index--;
      }
      return match;
    });

    if ('function' === typeof exports.formatArgs) {
      args = exports.formatArgs.apply(self, args);
    }
    var logFn = enabled.log || exports.log || console.log.bind(console);
    logFn.apply(self, args);
  }
  enabled.enabled = true;

  var fn = exports.enabled(namespace) ? enabled : disabled;

  fn.namespace = namespace;

  return fn;
}

/**
 * Enables a debug mode by namespaces. This can include modes
 * separated by a colon and wildcards.
 *
 * @param {String} namespaces
 * @api public
 */

function enable(namespaces) {
  exports.save(namespaces);

  var split = (namespaces || '').split(/[\s,]+/);
  var len = split.length;

  for (var i = 0; i < len; i++) {
    if (!split[i]) continue; // ignore empty strings
    namespaces = split[i].replace(/\*/g, '.*?');
    if (namespaces[0] === '-') {
      exports.skips.push(new RegExp('^' + namespaces.substr(1) + '$'));
    } else {
      exports.names.push(new RegExp('^' + namespaces + '$'));
    }
  }
}

/**
 * Disable debug output.
 *
 * @api public
 */

function disable() {
  exports.enable('');
}

/**
 * Returns true if the given mode name is enabled, false otherwise.
 *
 * @param {String} name
 * @return {Boolean}
 * @api public
 */

function enabled(name) {
  var i, len;
  for (i = 0, len = exports.skips.length; i < len; i++) {
    if (exports.skips[i].test(name)) {
      return false;
    }
  }
  for (i = 0, len = exports.names.length; i < len; i++) {
    if (exports.names[i].test(name)) {
      return true;
    }
  }
  return false;
}

/**
 * Coerce `val`.
 *
 * @param {Mixed} val
 * @return {Mixed}
 * @api private
 */

function coerce(val) {
  if (val instanceof Error) return val.stack || val.message;
  return val;
}

},{"ms":11}],11:[function(require,module,exports){
/**
 * Helpers.
 */

var s = 1000;
var m = s * 60;
var h = m * 60;
var d = h * 24;
var y = d * 365.25;

/**
 * Parse or format the given `val`.
 *
 * Options:
 *
 *  - `long` verbose formatting [false]
 *
 * @param {String|Number} val
 * @param {Object} options
 * @return {String|Number}
 * @api public
 */

module.exports = function(val, options){
  options = options || {};
  if ('string' == typeof val) return parse(val);
  return options.long
    ? long(val)
    : short(val);
};

/**
 * Parse the given `str` and return milliseconds.
 *
 * @param {String} str
 * @return {Number}
 * @api private
 */

function parse(str) {
  str = '' + str;
  if (str.length > 10000) return;
  var match = /^((?:\d+)?\.?\d+) *(milliseconds?|msecs?|ms|seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|years?|yrs?|y)?$/i.exec(str);
  if (!match) return;
  var n = parseFloat(match[1]);
  var type = (match[2] || 'ms').toLowerCase();
  switch (type) {
    case 'years':
    case 'year':
    case 'yrs':
    case 'yr':
    case 'y':
      return n * y;
    case 'days':
    case 'day':
    case 'd':
      return n * d;
    case 'hours':
    case 'hour':
    case 'hrs':
    case 'hr':
    case 'h':
      return n * h;
    case 'minutes':
    case 'minute':
    case 'mins':
    case 'min':
    case 'm':
      return n * m;
    case 'seconds':
    case 'second':
    case 'secs':
    case 'sec':
    case 's':
      return n * s;
    case 'milliseconds':
    case 'millisecond':
    case 'msecs':
    case 'msec':
    case 'ms':
      return n;
  }
}

/**
 * Short format for `ms`.
 *
 * @param {Number} ms
 * @return {String}
 * @api private
 */

function short(ms) {
  if (ms >= d) return Math.round(ms / d) + 'd';
  if (ms >= h) return Math.round(ms / h) + 'h';
  if (ms >= m) return Math.round(ms / m) + 'm';
  if (ms >= s) return Math.round(ms / s) + 's';
  return ms + 'ms';
}

/**
 * Long format for `ms`.
 *
 * @param {Number} ms
 * @return {String}
 * @api private
 */

function long(ms) {
  return plural(ms, d, 'day')
    || plural(ms, h, 'hour')
    || plural(ms, m, 'minute')
    || plural(ms, s, 'second')
    || ms + ' ms';
}

/**
 * Pluralization helper.
 */

function plural(ms, n, name) {
  if (ms < n) return;
  if (ms < n * 1.5) return Math.floor(ms / n) + ' ' + name;
  return Math.ceil(ms / n) + ' ' + name + 's';
}

},{}],12:[function(require,module,exports){
//     uuid.js
//
//     Copyright (c) 2010-2012 Robert Kieffer
//     MIT License - http://opensource.org/licenses/mit-license.php

(function() {
  var _global = this;

  // Unique ID creation requires a high quality random # generator.  We feature
  // detect to determine the best RNG source, normalizing to a function that
  // returns 128-bits of randomness, since that's what's usually required
  var _rng;

  // Node.js crypto-based RNG - http://nodejs.org/docs/v0.6.2/api/crypto.html
  //
  // Moderately fast, high quality
  if (typeof(_global.require) == 'function') {
    try {
      var _rb = _global.require('crypto').randomBytes;
      _rng = _rb && function() {return _rb(16);};
    } catch(e) {}
  }

  if (!_rng && _global.crypto && crypto.getRandomValues) {
    // WHATWG crypto-based RNG - http://wiki.whatwg.org/wiki/Crypto
    //
    // Moderately fast, high quality
    var _rnds8 = new Uint8Array(16);
    _rng = function whatwgRNG() {
      crypto.getRandomValues(_rnds8);
      return _rnds8;
    };
  }

  if (!_rng) {
    // Math.random()-based (RNG)
    //
    // If all else fails, use Math.random().  It's fast, but is of unspecified
    // quality.
    var  _rnds = new Array(16);
    _rng = function() {
      for (var i = 0, r; i < 16; i++) {
        if ((i & 0x03) === 0) r = Math.random() * 0x100000000;
        _rnds[i] = r >>> ((i & 0x03) << 3) & 0xff;
      }

      return _rnds;
    };
  }

  // Buffer class to use
  var BufferClass = typeof(_global.Buffer) == 'function' ? _global.Buffer : Array;

  // Maps for number <-> hex string conversion
  var _byteToHex = [];
  var _hexToByte = {};
  for (var i = 0; i < 256; i++) {
    _byteToHex[i] = (i + 0x100).toString(16).substr(1);
    _hexToByte[_byteToHex[i]] = i;
  }

  // **`parse()` - Parse a UUID into it's component bytes**
  function parse(s, buf, offset) {
    var i = (buf && offset) || 0, ii = 0;

    buf = buf || [];
    s.toLowerCase().replace(/[0-9a-f]{2}/g, function(oct) {
      if (ii < 16) { // Don't overflow!
        buf[i + ii++] = _hexToByte[oct];
      }
    });

    // Zero out remaining bytes if string was short
    while (ii < 16) {
      buf[i + ii++] = 0;
    }

    return buf;
  }

  // **`unparse()` - Convert UUID byte array (ala parse()) into a string**
  function unparse(buf, offset) {
    var i = offset || 0, bth = _byteToHex;
    return  bth[buf[i++]] + bth[buf[i++]] +
            bth[buf[i++]] + bth[buf[i++]] + '-' +
            bth[buf[i++]] + bth[buf[i++]] + '-' +
            bth[buf[i++]] + bth[buf[i++]] + '-' +
            bth[buf[i++]] + bth[buf[i++]] + '-' +
            bth[buf[i++]] + bth[buf[i++]] +
            bth[buf[i++]] + bth[buf[i++]] +
            bth[buf[i++]] + bth[buf[i++]];
  }

  // **`v1()` - Generate time-based UUID**
  //
  // Inspired by https://github.com/LiosK/UUID.js
  // and http://docs.python.org/library/uuid.html

  // random #'s we need to init node and clockseq
  var _seedBytes = _rng();

  // Per 4.5, create and 48-bit node id, (47 random bits + multicast bit = 1)
  var _nodeId = [
    _seedBytes[0] | 0x01,
    _seedBytes[1], _seedBytes[2], _seedBytes[3], _seedBytes[4], _seedBytes[5]
  ];

  // Per 4.2.2, randomize (14 bit) clockseq
  var _clockseq = (_seedBytes[6] << 8 | _seedBytes[7]) & 0x3fff;

  // Previous uuid creation time
  var _lastMSecs = 0, _lastNSecs = 0;

  // See https://github.com/broofa/node-uuid for API details
  function v1(options, buf, offset) {
    var i = buf && offset || 0;
    var b = buf || [];

    options = options || {};

    var clockseq = options.clockseq != null ? options.clockseq : _clockseq;

    // UUID timestamps are 100 nano-second units since the Gregorian epoch,
    // (1582-10-15 00:00).  JSNumbers aren't precise enough for this, so
    // time is handled internally as 'msecs' (integer milliseconds) and 'nsecs'
    // (100-nanoseconds offset from msecs) since unix epoch, 1970-01-01 00:00.
    var msecs = options.msecs != null ? options.msecs : new Date().getTime();

    // Per 4.2.1.2, use count of uuid's generated during the current clock
    // cycle to simulate higher resolution clock
    var nsecs = options.nsecs != null ? options.nsecs : _lastNSecs + 1;

    // Time since last uuid creation (in msecs)
    var dt = (msecs - _lastMSecs) + (nsecs - _lastNSecs)/10000;

    // Per 4.2.1.2, Bump clockseq on clock regression
    if (dt < 0 && options.clockseq == null) {
      clockseq = clockseq + 1 & 0x3fff;
    }

    // Reset nsecs if clock regresses (new clockseq) or we've moved onto a new
    // time interval
    if ((dt < 0 || msecs > _lastMSecs) && options.nsecs == null) {
      nsecs = 0;
    }

    // Per 4.2.1.2 Throw error if too many uuids are requested
    if (nsecs >= 10000) {
      throw new Error('uuid.v1(): Can\'t create more than 10M uuids/sec');
    }

    _lastMSecs = msecs;
    _lastNSecs = nsecs;
    _clockseq = clockseq;

    // Per 4.1.4 - Convert from unix epoch to Gregorian epoch
    msecs += 12219292800000;

    // `time_low`
    var tl = ((msecs & 0xfffffff) * 10000 + nsecs) % 0x100000000;
    b[i++] = tl >>> 24 & 0xff;
    b[i++] = tl >>> 16 & 0xff;
    b[i++] = tl >>> 8 & 0xff;
    b[i++] = tl & 0xff;

    // `time_mid`
    var tmh = (msecs / 0x100000000 * 10000) & 0xfffffff;
    b[i++] = tmh >>> 8 & 0xff;
    b[i++] = tmh & 0xff;

    // `time_high_and_version`
    b[i++] = tmh >>> 24 & 0xf | 0x10; // include version
    b[i++] = tmh >>> 16 & 0xff;

    // `clock_seq_hi_and_reserved` (Per 4.2.2 - include variant)
    b[i++] = clockseq >>> 8 | 0x80;

    // `clock_seq_low`
    b[i++] = clockseq & 0xff;

    // `node`
    var node = options.node || _nodeId;
    for (var n = 0; n < 6; n++) {
      b[i + n] = node[n];
    }

    return buf ? buf : unparse(b);
  }

  // **`v4()` - Generate random UUID**

  // See https://github.com/broofa/node-uuid for API details
  function v4(options, buf, offset) {
    // Deprecated - 'format' argument, as supported in v1.2
    var i = buf && offset || 0;

    if (typeof(options) == 'string') {
      buf = options == 'binary' ? new BufferClass(16) : null;
      options = null;
    }
    options = options || {};

    var rnds = options.random || (options.rng || _rng)();

    // Per 4.4, set bits for version and `clock_seq_hi_and_reserved`
    rnds[6] = (rnds[6] & 0x0f) | 0x40;
    rnds[8] = (rnds[8] & 0x3f) | 0x80;

    // Copy bytes to buffer, if provided
    if (buf) {
      for (var ii = 0; ii < 16; ii++) {
        buf[i + ii] = rnds[ii];
      }
    }

    return buf || unparse(rnds);
  }

  // Export public API
  var uuid = v4;
  uuid.v1 = v1;
  uuid.v4 = v4;
  uuid.parse = parse;
  uuid.unparse = unparse;
  uuid.BufferClass = BufferClass;

  if (typeof(module) != 'undefined' && module.exports) {
    // Publish as node.js module
    module.exports = uuid;
  } else  if (typeof define === 'function' && define.amd) {
    // Publish as AMD module
    define(function() {return uuid;});
 

  } else {
    // Publish as global (in browsers)
    var _previousRoot = _global.uuid;

    // **`noConflict()` - (browser only) to reset global 'uuid' var**
    uuid.noConflict = function() {
      _global.uuid = _previousRoot;
      return uuid;
    };

    _global.uuid = uuid;
  }
}).call(this);

},{}],13:[function(require,module,exports){
(function (global){
'use strict';

// Expose the Adapter function/object.
module.exports = Adapter;


// Dependencies

var browser = require('bowser'),
	debug = require('debug')('rtcninja:Adapter'),
	debugerror = require('debug')('rtcninja:ERROR:Adapter'),

	// Internal vars
	getUserMedia = null,
	RTCPeerConnection = null,
	RTCSessionDescription = null,
	RTCIceCandidate = null,
	MediaStreamTrack = null,
	getMediaDevices = null,
	attachMediaStream = null,
	canRenegotiate = false,
	oldSpecRTCOfferOptions = false,
	browserVersion = Number(browser.version) || 0,
	isDesktop = !!(!browser.mobile && !browser.tablet),
	hasWebRTC = false,
	virtGlobal, virtNavigator;

debugerror.log = console.warn.bind(console);

// Dirty trick to get this library working in a Node-webkit env with browserified libs
virtGlobal = global.window || global;
// Don't fail in Node
virtNavigator = virtGlobal.navigator || {};


// Constructor.

function Adapter(options) {
	// Chrome desktop, Chrome Android, Opera desktop, Opera Android, Android native browser
	// or generic Webkit browser.
	if (
		(isDesktop && browser.chrome && browserVersion >= 32) ||
		(browser.android && browser.chrome && browserVersion >= 39) ||
		(isDesktop && browser.opera && browserVersion >= 27) ||
		(browser.android && browser.opera && browserVersion >= 24) ||
		(browser.android && browser.webkit && !browser.chrome && browserVersion >= 37) ||
		(virtNavigator.webkitGetUserMedia && virtGlobal.webkitRTCPeerConnection)
	) {
		hasWebRTC = true;
		getUserMedia = virtNavigator.webkitGetUserMedia.bind(virtNavigator);
		RTCPeerConnection = virtGlobal.webkitRTCPeerConnection;
		RTCSessionDescription = virtGlobal.RTCSessionDescription;
		RTCIceCandidate = virtGlobal.RTCIceCandidate;
		MediaStreamTrack = virtGlobal.MediaStreamTrack;
		if (MediaStreamTrack && MediaStreamTrack.getSources) {
			getMediaDevices = MediaStreamTrack.getSources.bind(MediaStreamTrack);
		} else if (virtNavigator.getMediaDevices) {
			getMediaDevices = virtNavigator.getMediaDevices.bind(virtNavigator);
		}
		attachMediaStream = function (element, stream) {
			element.src = URL.createObjectURL(stream);
			return element;
		};
		canRenegotiate = true;
		oldSpecRTCOfferOptions = false;
	// Firefox desktop, Firefox Android.
	} else if (
		(isDesktop && browser.firefox && browserVersion >= 22) ||
		(browser.android && browser.firefox && browserVersion >= 33) ||
		(virtNavigator.mozGetUserMedia && virtGlobal.mozRTCPeerConnection)
	) {
		hasWebRTC = true;
		getUserMedia = virtNavigator.mozGetUserMedia.bind(virtNavigator);
		RTCPeerConnection = virtGlobal.mozRTCPeerConnection;
		RTCSessionDescription = virtGlobal.mozRTCSessionDescription;
		RTCIceCandidate = virtGlobal.mozRTCIceCandidate;
		MediaStreamTrack = virtGlobal.MediaStreamTrack;
		attachMediaStream = function (element, stream) {
			element.src = URL.createObjectURL(stream);
			return element;
		};
		canRenegotiate = false;
		oldSpecRTCOfferOptions = false;
		// WebRTC plugin required. For example IE or Safari with the Temasys plugin.
	} else if (
		options.plugin &&
		typeof options.plugin.isRequired === 'function' &&
		options.plugin.isRequired() &&
		typeof options.plugin.isInstalled === 'function' &&
		options.plugin.isInstalled()
	) {
		var pluginiface = options.plugin.interface;

		hasWebRTC = true;
		getUserMedia = pluginiface.getUserMedia;
		RTCPeerConnection = pluginiface.RTCPeerConnection;
		RTCSessionDescription = pluginiface.RTCSessionDescription;
		RTCIceCandidate = pluginiface.RTCIceCandidate;
		MediaStreamTrack = pluginiface.MediaStreamTrack;
		if (MediaStreamTrack && MediaStreamTrack.getSources) {
			getMediaDevices = MediaStreamTrack.getSources.bind(MediaStreamTrack);
		} else if (virtNavigator.getMediaDevices) {
			getMediaDevices = virtNavigator.getMediaDevices.bind(virtNavigator);
		}
		attachMediaStream = pluginiface.attachMediaStream;
		canRenegotiate = pluginiface.canRenegotiate;
		oldSpecRTCOfferOptions = true;  // TODO: Update when fixed in the plugin.
	// Best effort (may be adater.js is loaded).
	} else if (virtNavigator.getUserMedia && virtGlobal.RTCPeerConnection) {
		hasWebRTC = true;
		getUserMedia = virtNavigator.getUserMedia.bind(virtNavigator);
		RTCPeerConnection = virtGlobal.RTCPeerConnection;
		RTCSessionDescription = virtGlobal.RTCSessionDescription;
		RTCIceCandidate = virtGlobal.RTCIceCandidate;
		MediaStreamTrack = virtGlobal.MediaStreamTrack;
		if (MediaStreamTrack && MediaStreamTrack.getSources) {
			getMediaDevices = MediaStreamTrack.getSources.bind(MediaStreamTrack);
		} else if (virtNavigator.getMediaDevices) {
			getMediaDevices = virtNavigator.getMediaDevices.bind(virtNavigator);
		}
		attachMediaStream = virtGlobal.attachMediaStream || function (element, stream) {
			element.src = URL.createObjectURL(stream);
			return element;
		};
		canRenegotiate = false;
		oldSpecRTCOfferOptions = false;
	}


	function throwNonSupported(item) {
		return function () {
			throw new Error('rtcninja: WebRTC not supported, missing ' + item +
			' [browser: ' + browser.name + ' ' + browser.version + ']');
		};
	}


	// Public API.

	// Expose a WebRTC checker.
	Adapter.hasWebRTC = function () {
		return hasWebRTC;
	};

	// Expose getUserMedia.
	if (getUserMedia) {
		Adapter.getUserMedia = function (constraints, successCallback, errorCallback) {
			debug('getUserMedia() | constraints: %o', constraints);

			try {
				getUserMedia(constraints,
					function (stream) {
						debug('getUserMedia() | success');
						if (successCallback) {
							successCallback(stream);
						}
					},
					function (error) {
						debug('getUserMedia() | error:', error);
						if (errorCallback) {
							errorCallback(error);
						}
					}
				);
			}
			catch (error) {
				debugerror('getUserMedia() | error:', error);
				if (errorCallback) {
					errorCallback(error);
				}
			}
		};
	} else {
		Adapter.getUserMedia = function (constraints, successCallback, errorCallback) {
			debugerror('getUserMedia() | WebRTC not supported');
			if (errorCallback) {
				errorCallback(new Error('rtcninja: WebRTC not supported, missing ' +
				'getUserMedia [browser: ' + browser.name + ' ' + browser.version + ']'));
			} else {
				throwNonSupported('getUserMedia');
			}
		};
	}

	// Expose RTCPeerConnection.
	Adapter.RTCPeerConnection = RTCPeerConnection || throwNonSupported('RTCPeerConnection');

	// Expose RTCSessionDescription.
	Adapter.RTCSessionDescription = RTCSessionDescription || throwNonSupported('RTCSessionDescription');

	// Expose RTCIceCandidate.
	Adapter.RTCIceCandidate = RTCIceCandidate || throwNonSupported('RTCIceCandidate');

	// Expose MediaStreamTrack.
	Adapter.MediaStreamTrack = MediaStreamTrack || throwNonSupported('MediaStreamTrack');

	// Expose getMediaDevices.
	Adapter.getMediaDevices = getMediaDevices;

	// Expose MediaStreamTrack.
	Adapter.attachMediaStream = attachMediaStream || throwNonSupported('attachMediaStream');

	// Expose canRenegotiate attribute.
	Adapter.canRenegotiate = canRenegotiate;

	// Expose closeMediaStream.
	Adapter.closeMediaStream = function (stream) {
		if (!stream) {
			return;
		}

		// Latest spec states that MediaStream has no stop() method and instead must
		// call stop() on every MediaStreamTrack.
		if (MediaStreamTrack && MediaStreamTrack.prototype && MediaStreamTrack.prototype.stop) {
			debug('closeMediaStream() | calling stop() on all the MediaStreamTrack');

			var tracks, i, len;

			if (stream.getTracks) {
				tracks = stream.getTracks();
				for (i = 0, len = tracks.length; i < len; i += 1) {
					tracks[i].stop();
				}
			} else {
				tracks = stream.getAudioTracks();
				for (i = 0, len = tracks.length; i < len; i += 1) {
					tracks[i].stop();
				}

				tracks = stream.getVideoTracks();
				for (i = 0, len = tracks.length; i < len; i += 1) {
					tracks[i].stop();
				}
			}
		// Deprecated by the spec, but still in use.
		} else if (typeof stream.stop === 'function') {
			debug('closeMediaStream() | calling stop() on the MediaStream');

			stream.stop();
		}
	};

	// Expose fixPeerConnectionConfig.
	Adapter.fixPeerConnectionConfig = function (pcConfig) {
		var i, len, iceServer, hasUrls, hasUrl;

		if (!Array.isArray(pcConfig.iceServers)) {
			pcConfig.iceServers = [];
		}

		for (i = 0, len = pcConfig.iceServers.length; i < len; i += 1) {
			iceServer = pcConfig.iceServers[i];
			hasUrls = iceServer.hasOwnProperty('urls');
			hasUrl = iceServer.hasOwnProperty('url');

			if (typeof iceServer === 'object') {
				// Has .urls but not .url, so add .url with a single string value.
				if (hasUrls && !hasUrl) {
					iceServer.url = (Array.isArray(iceServer.urls) ? iceServer.urls[0] : iceServer.urls);
				// Has .url but not .urls, so add .urls with same value.
				} else if (!hasUrls && hasUrl) {
					iceServer.urls = (Array.isArray(iceServer.url) ? iceServer.url.slice() : iceServer.url);
				}

				// Ensure .url is a single string.
				if (hasUrl && Array.isArray(iceServer.url)) {
					iceServer.url = iceServer.url[0];
				}
			}
		}
	};

	// Expose fixRTCOfferOptions.
	Adapter.fixRTCOfferOptions = function (options) {
		options = options || {};

		// New spec.
		if (!oldSpecRTCOfferOptions) {
			if (options.mandatory && options.mandatory.OfferToReceiveAudio) {
				options.offerToReceiveAudio = 1;
			}
			if (options.mandatory && options.mandatory.OfferToReceiveVideo) {
				options.offerToReceiveVideo = 1;
			}
			delete options.mandatory;
		// Old spec.
		} else {
			if (options.offerToReceiveAudio) {
				options.mandatory = options.mandatory || {};
				options.mandatory.OfferToReceiveAudio = true;
			}
			if (options.offerToReceiveVideo) {
				options.mandatory = options.mandatory || {};
				options.mandatory.OfferToReceiveVideo = true;
			}
		}
	};

	return Adapter;
}

}).call(this,typeof global !== "undefined" ? global : typeof self !== "undefined" ? self : typeof window !== "undefined" ? window : {})

},{"bowser":17,"debug":9}],14:[function(require,module,exports){
'use strict';

// Expose the RTCPeerConnection class.
module.exports = RTCPeerConnection;


// Dependencies.

var merge = require('merge'),
	debug = require('debug')('rtcninja:RTCPeerConnection'),
	debugerror = require('debug')('rtcninja:ERROR:RTCPeerConnection'),
	Adapter = require('./Adapter'),

	// Internal constants.
	C = {
		REGEXP_NORMALIZED_CANDIDATE: new RegExp(/^candidate:/i),
		REGEXP_FIX_CANDIDATE: new RegExp(/(^a=|\r|\n)/gi),
		REGEXP_RELAY_CANDIDATE: new RegExp(/ relay /i),
		REGEXP_SDP_CANDIDATES: new RegExp(/^a=candidate:.*\r\n/igm),
		REGEXP_SDP_NON_RELAY_CANDIDATES: new RegExp(/^a=candidate:(.(?!relay ))*\r\n/igm)
	},

	// Internal variables.
	VAR = {
		normalizeCandidate: null
	};

debugerror.log = console.warn.bind(console);


// Constructor

function RTCPeerConnection(pcConfig, pcConstraints) {
	debug('new | pcConfig: %o', pcConfig);

	// Set this.pcConfig and this.options.
	setConfigurationAndOptions.call(this, pcConfig);

	// NOTE: Deprecated pcConstraints argument.
	this.pcConstraints = pcConstraints;

	// Own version of the localDescription.
	this.ourLocalDescription = null;

	// Latest values of PC attributes to avoid events with same value.
	this.ourSignalingState = null;
	this.ourIceConnectionState = null;
	this.ourIceGatheringState = null;

	// Timer for options.gatheringTimeout.
	this.timerGatheringTimeout = null;

	// Timer for options.gatheringTimeoutAfterRelay.
	this.timerGatheringTimeoutAfterRelay = null;

	// Flag to ignore new gathered ICE candidates.
	this.ignoreIceGathering = false;

	// Flag set when closed.
	this.closed = false;

	// Set RTCPeerConnection.
	setPeerConnection.call(this);

	// Set properties.
	setProperties.call(this);
}


// Public API.

RTCPeerConnection.prototype.createOffer = function (successCallback, failureCallback, options) {
	debug('createOffer()');

	var self = this;

	Adapter.fixRTCOfferOptions(options);

	this.pc.createOffer(
		function (offer) {
			if (isClosed.call(self)) {
				return;
			}
			debug('createOffer() | success');
			if (successCallback) {
				successCallback(offer);
			}
		},
		function (error) {
			if (isClosed.call(self)) {
				return;
			}
			debugerror('createOffer() | error:', error);
			if (failureCallback) {
				failureCallback(error);
			}
		},
		options
	);
};


RTCPeerConnection.prototype.createAnswer = function (successCallback, failureCallback, options) {
	debug('createAnswer()');

	var self = this;

	this.pc.createAnswer(
		function (answer) {
			if (isClosed.call(self)) {
				return;
			}
			debug('createAnswer() | success');
			if (successCallback) {
				successCallback(answer);
			}
		},
		function (error) {
			if (isClosed.call(self)) {
				return;
			}
			debugerror('createAnswer() | error:', error);
			if (failureCallback) {
				failureCallback(error);
			}
		},
		options
	);
};


RTCPeerConnection.prototype.setLocalDescription = function (description, successCallback, failureCallback) {
	debug('setLocalDescription()');

	var self = this;

	this.pc.setLocalDescription(
		description,
		// success.
		function () {
			if (isClosed.call(self)) {
				return;
			}
			debug('setLocalDescription() | success');

			// Clear gathering timers.
			clearTimeout(self.timerGatheringTimeout);
			delete self.timerGatheringTimeout;
			clearTimeout(self.timerGatheringTimeoutAfterRelay);
			delete self.timerGatheringTimeoutAfterRelay;

			runTimerGatheringTimeout();
			if (successCallback) {
				successCallback();
			}
		},
		// failure
		function (error) {
			if (isClosed.call(self)) {
				return;
			}
			debugerror('setLocalDescription() | error:', error);
			if (failureCallback) {
				failureCallback(error);
			}
		}
	);

	// Enable (again) ICE gathering.
	this.ignoreIceGathering = false;

	// Handle gatheringTimeout.
	function runTimerGatheringTimeout() {
		if (typeof self.options.gatheringTimeout !== 'number') {
			return;
		}
		// If setLocalDescription was already called, it may happen that
		// ICE gathering is not needed, so don't run this timer.
		if (self.pc.iceGatheringState === 'complete') {
			return;
		}

		debug('setLocalDescription() | ending gathering in %d ms (gatheringTimeout option)',
			self.options.gatheringTimeout);

		self.timerGatheringTimeout = setTimeout(function () {
			if (isClosed.call(self)) {
				return;
			}

			debug('forced end of candidates after gatheringTimeout timeout');

			// Clear gathering timers.
			delete self.timerGatheringTimeout;
			clearTimeout(self.timerGatheringTimeoutAfterRelay);
			delete self.timerGatheringTimeoutAfterRelay;

			// Ignore new candidates.
			self.ignoreIceGathering = true;
			if (self.onicecandidate) {
				self.onicecandidate({ candidate: null }, null);
			}

		}, self.options.gatheringTimeout);
	}
};


RTCPeerConnection.prototype.setRemoteDescription = function (description, successCallback, failureCallback) {
	debug('setRemoteDescription()');

	var self = this;

	this.pc.setRemoteDescription(
		description,
		function () {
			if (isClosed.call(self)) {
				return;
			}
			debug('setRemoteDescription() | success');
			if (successCallback) {
				successCallback();
			}
		},
		function (error) {
			if (isClosed.call(self)) {
				return;
			}
			debugerror('setRemoteDescription() | error:', error);
			if (failureCallback) {
				failureCallback(error);
			}
		}
	);
};


RTCPeerConnection.prototype.updateIce = function (pcConfig) {
	debug('updateIce() | pcConfig: %o', pcConfig);

	// Update this.pcConfig and this.options.
	setConfigurationAndOptions.call(this, pcConfig);

	this.pc.updateIce(this.pcConfig);

	// Enable (again) ICE gathering.
	this.ignoreIceGathering = false;
};


RTCPeerConnection.prototype.addIceCandidate = function (candidate, successCallback, failureCallback) {
	debug('addIceCandidate() | candidate: %o', candidate);

	var self = this;

	this.pc.addIceCandidate(
		candidate,
		function () {
			if (isClosed.call(self)) {
				return;
			}
			debug('addIceCandidate() | success');
			if (successCallback) {
				successCallback();
			}
		},
		function (error) {
			if (isClosed.call(self)) {
				return;
			}
			debugerror('addIceCandidate() | error:', error);
			if (failureCallback) {
				failureCallback(error);
			}
		}
	);
};


RTCPeerConnection.prototype.getConfiguration = function () {
	debug('getConfiguration()');

	return this.pc.getConfiguration();
};


RTCPeerConnection.prototype.getLocalStreams = function () {
	debug('getLocalStreams()');

	return this.pc.getLocalStreams();
};


RTCPeerConnection.prototype.getRemoteStreams = function () {
	debug('getRemoteStreams()');

	return this.pc.getRemoteStreams();
};


RTCPeerConnection.prototype.getStreamById = function (streamId) {
	debug('getStreamById() | streamId: %s', streamId);

	return this.pc.getStreamById(streamId);
};


RTCPeerConnection.prototype.addStream = function (stream) {
	debug('addStream() | stream: %s', stream);

	this.pc.addStream(stream);
};


RTCPeerConnection.prototype.removeStream = function (stream) {
	debug('removeStream() | stream: %o', stream);

	this.pc.removeStream(stream);
};


RTCPeerConnection.prototype.close = function () {
	debug('close()');

	this.closed = true;

	// Clear gathering timers.
	clearTimeout(this.timerGatheringTimeout);
	delete this.timerGatheringTimeout;
	clearTimeout(this.timerGatheringTimeoutAfterRelay);
	delete this.timerGatheringTimeoutAfterRelay;

	this.pc.close();
};


RTCPeerConnection.prototype.createDataChannel = function () {
	debug('createDataChannel()');

	return this.pc.createDataChannel.apply(this.pc, arguments);
};


RTCPeerConnection.prototype.createDTMFSender = function (track) {
	debug('createDTMFSender()');

	return this.pc.createDTMFSender(track);
};


RTCPeerConnection.prototype.getStats = function () {
	debug('getStats()');

	return this.pc.getStats.apply(this.pc, arguments);
};


RTCPeerConnection.prototype.setIdentityProvider = function () {
	debug('setIdentityProvider()');

	return this.pc.setIdentityProvider.apply(this.pc, arguments);
};


RTCPeerConnection.prototype.getIdentityAssertion = function () {
	debug('getIdentityAssertion()');

	return this.pc.getIdentityAssertion();
};


RTCPeerConnection.prototype.reset = function (pcConfig) {
	debug('reset() | pcConfig: %o', pcConfig);

	var pc = this.pc;

	// Remove events in the old PC.
	pc.onnegotiationneeded = null;
	pc.onicecandidate = null;
	pc.onaddstream = null;
	pc.onremovestream = null;
	pc.ondatachannel = null;
	pc.onsignalingstatechange = null;
	pc.oniceconnectionstatechange = null;
	pc.onicegatheringstatechange = null;
	pc.onidentityresult = null;
	pc.onpeeridentity = null;
	pc.onidpassertionerror = null;
	pc.onidpvalidationerror = null;

	// Clear gathering timers.
	clearTimeout(this.timerGatheringTimeout);
	delete this.timerGatheringTimeout;
	clearTimeout(this.timerGatheringTimeoutAfterRelay);
	delete this.timerGatheringTimeoutAfterRelay;

	// Silently close the old PC.
	debug('reset() | closing current peerConnection');
	pc.close();

	// Set this.pcConfig and this.options.
	setConfigurationAndOptions.call(this, pcConfig);

	// Create a new PC.
	setPeerConnection.call(this);
};


// Private Helpers.

function setConfigurationAndOptions(pcConfig) {
	// Clone pcConfig.
	this.pcConfig = merge(true, pcConfig);

	// Fix pcConfig.
	Adapter.fixPeerConnectionConfig(this.pcConfig);

	this.options = {
		iceTransportsRelay: (this.pcConfig.iceTransports === 'relay'),
		iceTransportsNone: (this.pcConfig.iceTransports === 'none'),
		gatheringTimeout: this.pcConfig.gatheringTimeout,
		gatheringTimeoutAfterRelay: this.pcConfig.gatheringTimeoutAfterRelay
	};

	// Remove custom rtcninja.RTCPeerConnection options from pcConfig.
	delete this.pcConfig.gatheringTimeout;
	delete this.pcConfig.gatheringTimeoutAfterRelay;

	debug('setConfigurationAndOptions | processed pcConfig: %o', this.pcConfig);
}


function isClosed() {
	return ((this.closed) || (this.pc && this.pc.iceConnectionState === 'closed'));
}


function setEvents() {
	var self = this,
		pc = this.pc;

	pc.onnegotiationneeded = function (event) {
		if (isClosed.call(self)) {
			return;
		}

		debug('onnegotiationneeded()');
		if (self.onnegotiationneeded) {
			self.onnegotiationneeded(event);
		}
	};

	pc.onicecandidate = function (event) {
		var candidate, isRelay, newCandidate;

		if (isClosed.call(self)) {
			return;
		}
		if (self.ignoreIceGathering) {
			return;
		}

		// Ignore any candidate (event the null one) if iceTransports:'none' is set.
		if (self.options.iceTransportsNone) {
			return;
		}

		candidate = event.candidate;

		if (candidate) {
			isRelay = C.REGEXP_RELAY_CANDIDATE.test(candidate.candidate);

			// Ignore if just relay candidates are requested.
			if (self.options.iceTransportsRelay && !isRelay) {
				return;
			}

			// Handle gatheringTimeoutAfterRelay.
			if (isRelay && !self.timerGatheringTimeoutAfterRelay &&
				(typeof self.options.gatheringTimeoutAfterRelay === 'number')) {
				debug('onicecandidate() | first relay candidate found, ending gathering in %d ms', self.options.gatheringTimeoutAfterRelay);

				self.timerGatheringTimeoutAfterRelay = setTimeout(function () {
					if (isClosed.call(self)) {
						return;
					}

					debug('forced end of candidates after timeout');

					// Clear gathering timers.
					delete self.timerGatheringTimeoutAfterRelay;
					clearTimeout(self.timerGatheringTimeout);
					delete self.timerGatheringTimeout;

					// Ignore new candidates.
					self.ignoreIceGathering = true;
					if (self.onicecandidate) {
						self.onicecandidate({candidate: null}, null);
					}
				}, self.options.gatheringTimeoutAfterRelay);
			}

			newCandidate = new Adapter.RTCIceCandidate({
				sdpMid: candidate.sdpMid,
				sdpMLineIndex: candidate.sdpMLineIndex,
				candidate: candidate.candidate
			});

			// Force correct candidate syntax (just check it once).
			if (VAR.normalizeCandidate === null) {
				if (C.REGEXP_NORMALIZED_CANDIDATE.test(candidate.candidate)) {
					VAR.normalizeCandidate = false;
				} else {
					debug('onicecandidate() | normalizing ICE candidates syntax (remove "a=" and "\\r\\n")');
					VAR.normalizeCandidate = true;
				}
			}
			if (VAR.normalizeCandidate) {
				newCandidate.candidate = candidate.candidate.replace(C.REGEXP_FIX_CANDIDATE, '');
			}

			debug(
				'onicecandidate() | m%d(%s) %s',
				newCandidate.sdpMLineIndex,
				newCandidate.sdpMid || 'no mid', newCandidate.candidate);
			if (self.onicecandidate) {
				self.onicecandidate(event, newCandidate);
			}
		// Null candidate (end of candidates).
		} else {
			debug('onicecandidate() | end of candidates');

			// Clear gathering timers.
			clearTimeout(self.timerGatheringTimeout);
			delete self.timerGatheringTimeout;
			clearTimeout(self.timerGatheringTimeoutAfterRelay);
			delete self.timerGatheringTimeoutAfterRelay;
			if (self.onicecandidate) {
				self.onicecandidate(event, null);
			}
		}
	};

	pc.onaddstream = function (event) {
		if (isClosed.call(self)) {
			return;
		}

		debug('onaddstream() | stream: %o', event.stream);
		if (self.onaddstream) {
			self.onaddstream(event, event.stream);
		}
	};

	pc.onremovestream = function (event) {
		if (isClosed.call(self)) {
			return;
		}

		debug('onremovestream() | stream: %o', event.stream);
		if (self.onremovestream) {
			self.onremovestream(event, event.stream);
		}
	};

	pc.ondatachannel = function (event) {
		if (isClosed.call(self)) {
			return;
		}

		debug('ondatachannel() | datachannel: %o', event.channel);
		if (self.ondatachannel) {
			self.ondatachannel(event, event.channel);
		}
	};

	pc.onsignalingstatechange = function (event) {
		if (pc.signalingState === self.ourSignalingState) {
			return;
		}

		debug('onsignalingstatechange() | signalingState: %s', pc.signalingState);
		self.ourSignalingState = pc.signalingState;
		if (self.onsignalingstatechange) {
			self.onsignalingstatechange(event, pc.signalingState);
		}
	};

	pc.oniceconnectionstatechange = function (event) {
		if (pc.iceConnectionState === self.ourIceConnectionState) {
			return;
		}

		debug('oniceconnectionstatechange() | iceConnectionState: %s', pc.iceConnectionState);
		self.ourIceConnectionState = pc.iceConnectionState;
		if (self.oniceconnectionstatechange) {
			self.oniceconnectionstatechange(event, pc.iceConnectionState);
		}
	};

	pc.onicegatheringstatechange = function (event) {
		if (isClosed.call(self)) {
			return;
		}

		if (pc.iceGatheringState === self.ourIceGatheringState) {
			return;
		}

		debug('onicegatheringstatechange() | iceGatheringState: %s', pc.iceGatheringState);
		self.ourIceGatheringState = pc.iceGatheringState;
		if (self.onicegatheringstatechange) {
			self.onicegatheringstatechange(event, pc.iceGatheringState);
		}
	};

	pc.onidentityresult = function (event) {
		if (isClosed.call(self)) {
			return;
		}

		debug('onidentityresult()');
		if (self.onidentityresult) {
			self.onidentityresult(event);
		}
	};

	pc.onpeeridentity = function (event) {
		if (isClosed.call(self)) {
			return;
		}

		debug('onpeeridentity()');
		if (self.onpeeridentity) {
			self.onpeeridentity(event);
		}
	};

	pc.onidpassertionerror = function (event) {
		if (isClosed.call(self)) {
			return;
		}

		debug('onidpassertionerror()');
		if (self.onidpassertionerror) {
			self.onidpassertionerror(event);
		}
	};

	pc.onidpvalidationerror = function (event) {
		if (isClosed.call(self)) {
			return;
		}

		debug('onidpvalidationerror()');
		if (self.onidpvalidationerror) {
			self.onidpvalidationerror(event);
		}
	};
}


function setPeerConnection() {
	// Create a RTCPeerConnection.
	if (!this.pcConstraints) {
		this.pc = new Adapter.RTCPeerConnection(this.pcConfig);
	} else {
		// NOTE: Deprecated.
		this.pc = new Adapter.RTCPeerConnection(this.pcConfig, this.pcConstraints);
	}

	// Set RTC events.
	setEvents.call(this);
}


function getLocalDescription() {
	var pc = this.pc,
		options = this.options,
		sdp = null;

	if (!pc.localDescription) {
		this.ourLocalDescription = null;
		return null;
	}

	// Mangle the SDP string.
	if (options.iceTransportsRelay) {
		sdp = pc.localDescription.sdp.replace(C.REGEXP_SDP_NON_RELAY_CANDIDATES, '');
	} else if (options.iceTransportsNone) {
		sdp = pc.localDescription.sdp.replace(C.REGEXP_SDP_CANDIDATES, '');
	}

	this.ourLocalDescription = new Adapter.RTCSessionDescription({
		type: pc.localDescription.type,
		sdp: sdp || pc.localDescription.sdp
	});

	return this.ourLocalDescription;
}


function setProperties() {
	var self = this;

	Object.defineProperties(this, {
		peerConnection: {
			get: function () {
				return self.pc;
			}
		},

		signalingState: {
			get: function () {
				return self.pc.signalingState;
			}
		},

		iceConnectionState: {
			get: function () {
				return self.pc.iceConnectionState;
			}
		},

		iceGatheringState: {
			get: function () {
				return self.pc.iceGatheringState;
			}
		},

		localDescription: {
			get: function () {
				return getLocalDescription.call(self);
			}
		},

		remoteDescription: {
			get: function () {
				return self.pc.remoteDescription;
			}
		},

		peerIdentity: {
			get: function () {
				return self.pc.peerIdentity;
			}
		}
	});
}

},{"./Adapter":13,"debug":9,"merge":18}],15:[function(require,module,exports){
'use strict';

module.exports = rtcninja;


// Dependencies.

var browser = require('bowser'),
	debug = require('debug')('rtcninja'),
	debugerror = require('debug')('rtcninja:ERROR'),
	version = require('./version'),
	Adapter = require('./Adapter'),
	RTCPeerConnection = require('./RTCPeerConnection'),

	// Internal vars.
	called = false;

debugerror.log = console.warn.bind(console);
debug('version %s', version);
debug('detected browser: %s %s [mobile:%s, tablet:%s, android:%s, ios:%s]',
		browser.name, browser.version, !!browser.mobile, !!browser.tablet,
		!!browser.android, !!browser.ios);


// Constructor.

function rtcninja(options) {
	// Load adapter
	var iface = Adapter(options || {});  // jshint ignore:line

	called = true;

	// Expose RTCPeerConnection class.
	rtcninja.RTCPeerConnection = RTCPeerConnection;

	// Expose WebRTC API and utils.
	rtcninja.getUserMedia = iface.getUserMedia;
	rtcninja.RTCSessionDescription = iface.RTCSessionDescription;
	rtcninja.RTCIceCandidate = iface.RTCIceCandidate;
	rtcninja.MediaStreamTrack = iface.MediaStreamTrack;
	rtcninja.getMediaDevices = iface.getMediaDevices;
	rtcninja.attachMediaStream = iface.attachMediaStream;
	rtcninja.closeMediaStream = iface.closeMediaStream;
	rtcninja.canRenegotiate = iface.canRenegotiate;

	// Log WebRTC support.
	if (iface.hasWebRTC()) {
		debug('WebRTC supported');
		return true;
	} else {
		debugerror('WebRTC not supported');
		return false;
	}
}


// Public API.

// If called without calling rtcninja(), call it.
rtcninja.hasWebRTC = function () {
	if (!called) {
		rtcninja();
	}

	return Adapter.hasWebRTC();
};


// Expose version property.
Object.defineProperty(rtcninja, 'version', {
	get: function () {
		return version;
	}
});


// Expose called property.
Object.defineProperty(rtcninja, 'called', {
	get: function () {
		return called;
	}
});


// Exposing stuff.

rtcninja.debug = require('debug');
rtcninja.browser = browser;

},{"./Adapter":13,"./RTCPeerConnection":14,"./version":16,"bowser":17,"debug":9}],16:[function(require,module,exports){
'use strict';

// Expose the 'version' field of package.json.
module.exports = require('../package.json').version;


},{"../package.json":19}],17:[function(require,module,exports){
/*!
  * Bowser - a browser detector
  * https://github.com/ded/bowser
  * MIT License | (c) Dustin Diaz 2015
  */

!function (name, definition) {
  if (typeof module != 'undefined' && module.exports) module.exports = definition()
  else if (typeof define == 'function' && define.amd) define(definition)
  else this[name] = definition()
}('bowser', function () {
  /**
    * See useragents.js for examples of navigator.userAgent
    */

  var t = true

  function detect(ua) {

    function getFirstMatch(regex) {
      var match = ua.match(regex);
      return (match && match.length > 1 && match[1]) || '';
    }

    function getSecondMatch(regex) {
      var match = ua.match(regex);
      return (match && match.length > 1 && match[2]) || '';
    }

    var iosdevice = getFirstMatch(/(ipod|iphone|ipad)/i).toLowerCase()
      , likeAndroid = /like android/i.test(ua)
      , android = !likeAndroid && /android/i.test(ua)
      , chromeBook = /CrOS/.test(ua)
      , edgeVersion = getFirstMatch(/edge\/(\d+(\.\d+)?)/i)
      , versionIdentifier = getFirstMatch(/version\/(\d+(\.\d+)?)/i)
      , tablet = /tablet/i.test(ua)
      , mobile = !tablet && /[^-]mobi/i.test(ua)
      , result

    if (/opera|opr/i.test(ua)) {
      result = {
        name: 'Opera'
      , opera: t
      , version: versionIdentifier || getFirstMatch(/(?:opera|opr)[\s\/](\d+(\.\d+)?)/i)
      }
    }
    else if (/yabrowser/i.test(ua)) {
      result = {
        name: 'Yandex Browser'
      , yandexbrowser: t
      , version: versionIdentifier || getFirstMatch(/(?:yabrowser)[\s\/](\d+(\.\d+)?)/i)
      }
    }
    else if (/windows phone/i.test(ua)) {
      result = {
        name: 'Windows Phone'
      , windowsphone: t
      }
      if (edgeVersion) {
        result.msedge = t
        result.version = edgeVersion
      }
      else {
        result.msie = t
        result.version = getFirstMatch(/iemobile\/(\d+(\.\d+)?)/i)
      }
    }
    else if (/msie|trident/i.test(ua)) {
      result = {
        name: 'Internet Explorer'
      , msie: t
      , version: getFirstMatch(/(?:msie |rv:)(\d+(\.\d+)?)/i)
      }
    } else if (chromeBook) {
      result = {
        name: 'Chrome'
      , chromeBook: t
      , chrome: t
      , version: getFirstMatch(/(?:chrome|crios|crmo)\/(\d+(\.\d+)?)/i)
      }
    } else if (/chrome.+? edge/i.test(ua)) {
      result = {
        name: 'Microsoft Edge'
      , msedge: t
      , version: edgeVersion
      }
    }
    else if (/chrome|crios|crmo/i.test(ua)) {
      result = {
        name: 'Chrome'
      , chrome: t
      , version: getFirstMatch(/(?:chrome|crios|crmo)\/(\d+(\.\d+)?)/i)
      }
    }
    else if (iosdevice) {
      result = {
        name : iosdevice == 'iphone' ? 'iPhone' : iosdevice == 'ipad' ? 'iPad' : 'iPod'
      }
      // WTF: version is not part of user agent in web apps
      if (versionIdentifier) {
        result.version = versionIdentifier
      }
    }
    else if (/sailfish/i.test(ua)) {
      result = {
        name: 'Sailfish'
      , sailfish: t
      , version: getFirstMatch(/sailfish\s?browser\/(\d+(\.\d+)?)/i)
      }
    }
    else if (/seamonkey\//i.test(ua)) {
      result = {
        name: 'SeaMonkey'
      , seamonkey: t
      , version: getFirstMatch(/seamonkey\/(\d+(\.\d+)?)/i)
      }
    }
    else if (/firefox|iceweasel/i.test(ua)) {
      result = {
        name: 'Firefox'
      , firefox: t
      , version: getFirstMatch(/(?:firefox|iceweasel)[ \/](\d+(\.\d+)?)/i)
      }
      if (/\((mobile|tablet);[^\)]*rv:[\d\.]+\)/i.test(ua)) {
        result.firefoxos = t
      }
    }
    else if (/silk/i.test(ua)) {
      result =  {
        name: 'Amazon Silk'
      , silk: t
      , version : getFirstMatch(/silk\/(\d+(\.\d+)?)/i)
      }
    }
    else if (android) {
      result = {
        name: 'Android'
      , version: versionIdentifier
      }
    }
    else if (/phantom/i.test(ua)) {
      result = {
        name: 'PhantomJS'
      , phantom: t
      , version: getFirstMatch(/phantomjs\/(\d+(\.\d+)?)/i)
      }
    }
    else if (/blackberry|\bbb\d+/i.test(ua) || /rim\stablet/i.test(ua)) {
      result = {
        name: 'BlackBerry'
      , blackberry: t
      , version: versionIdentifier || getFirstMatch(/blackberry[\d]+\/(\d+(\.\d+)?)/i)
      }
    }
    else if (/(web|hpw)os/i.test(ua)) {
      result = {
        name: 'WebOS'
      , webos: t
      , version: versionIdentifier || getFirstMatch(/w(?:eb)?osbrowser\/(\d+(\.\d+)?)/i)
      };
      /touchpad\//i.test(ua) && (result.touchpad = t)
    }
    else if (/bada/i.test(ua)) {
      result = {
        name: 'Bada'
      , bada: t
      , version: getFirstMatch(/dolfin\/(\d+(\.\d+)?)/i)
      };
    }
    else if (/tizen/i.test(ua)) {
      result = {
        name: 'Tizen'
      , tizen: t
      , version: getFirstMatch(/(?:tizen\s?)?browser\/(\d+(\.\d+)?)/i) || versionIdentifier
      };
    }
    else if (/safari/i.test(ua)) {
      result = {
        name: 'Safari'
      , safari: t
      , version: versionIdentifier
      }
    }
    else {
      result = {
        name: getFirstMatch(/^(.*)\/(.*) /),
        version: getSecondMatch(/^(.*)\/(.*) /)
     };
   }

    // set webkit or gecko flag for browsers based on these engines
    if (!result.msedge && /(apple)?webkit/i.test(ua)) {
      result.name = result.name || "Webkit"
      result.webkit = t
      if (!result.version && versionIdentifier) {
        result.version = versionIdentifier
      }
    } else if (!result.opera && /gecko\//i.test(ua)) {
      result.name = result.name || "Gecko"
      result.gecko = t
      result.version = result.version || getFirstMatch(/gecko\/(\d+(\.\d+)?)/i)
    }

    // set OS flags for platforms that have multiple browsers
    if (!result.msedge && (android || result.silk)) {
      result.android = t
    } else if (iosdevice) {
      result[iosdevice] = t
      result.ios = t
    }

    // OS version extraction
    var osVersion = '';
    if (result.windowsphone) {
      osVersion = getFirstMatch(/windows phone (?:os)?\s?(\d+(\.\d+)*)/i);
    } else if (iosdevice) {
      osVersion = getFirstMatch(/os (\d+([_\s]\d+)*) like mac os x/i);
      osVersion = osVersion.replace(/[_\s]/g, '.');
    } else if (android) {
      osVersion = getFirstMatch(/android[ \/-](\d+(\.\d+)*)/i);
    } else if (result.webos) {
      osVersion = getFirstMatch(/(?:web|hpw)os\/(\d+(\.\d+)*)/i);
    } else if (result.blackberry) {
      osVersion = getFirstMatch(/rim\stablet\sos\s(\d+(\.\d+)*)/i);
    } else if (result.bada) {
      osVersion = getFirstMatch(/bada\/(\d+(\.\d+)*)/i);
    } else if (result.tizen) {
      osVersion = getFirstMatch(/tizen[\/\s](\d+(\.\d+)*)/i);
    }
    if (osVersion) {
      result.osversion = osVersion;
    }

    // device type extraction
    var osMajorVersion = osVersion.split('.')[0];
    if (tablet || iosdevice == 'ipad' || (android && (osMajorVersion == 3 || (osMajorVersion == 4 && !mobile))) || result.silk) {
      result.tablet = t
    } else if (mobile || iosdevice == 'iphone' || iosdevice == 'ipod' || android || result.blackberry || result.webos || result.bada) {
      result.mobile = t
    }

    // Graded Browser Support
    // http://developer.yahoo.com/yui/articles/gbs
    if (result.msedge ||
        (result.msie && result.version >= 10) ||
        (result.yandexbrowser && result.version >= 15) ||
        (result.chrome && result.version >= 20) ||
        (result.firefox && result.version >= 20.0) ||
        (result.safari && result.version >= 6) ||
        (result.opera && result.version >= 10.0) ||
        (result.ios && result.osversion && result.osversion.split(".")[0] >= 6) ||
        (result.blackberry && result.version >= 10.1)
        ) {
      result.a = t;
    }
    else if ((result.msie && result.version < 10) ||
        (result.chrome && result.version < 20) ||
        (result.firefox && result.version < 20.0) ||
        (result.safari && result.version < 6) ||
        (result.opera && result.version < 10.0) ||
        (result.ios && result.osversion && result.osversion.split(".")[0] < 6)
        ) {
      result.c = t
    } else result.x = t

    return result
  }

  var bowser = detect(typeof navigator !== 'undefined' ? navigator.userAgent : '')

  bowser.test = function (browserList) {
    for (var i = 0; i < browserList.length; ++i) {
      var browserItem = browserList[i];
      if (typeof browserItem=== 'string') {
        if (browserItem in bowser) {
          return true;
        }
      }
    }
    return false;
  }

  /*
   * Set our detect method to the main bowser object so we can
   * reuse it to test other user agents.
   * This is needed to implement future tests.
   */
  bowser._detect = detect;

  return bowser
});

},{}],18:[function(require,module,exports){
/*!
 * @name JavaScript/NodeJS Merge v1.2.0
 * @author yeikos
 * @repository https://github.com/yeikos/js.merge

 * Copyright 2014 yeikos - MIT license
 * https://raw.github.com/yeikos/js.merge/master/LICENSE
 */

;(function(isNode) {

	/**
	 * Merge one or more objects 
	 * @param bool? clone
	 * @param mixed,... arguments
	 * @return object
	 */

	var Public = function(clone) {

		return merge(clone === true, false, arguments);

	}, publicName = 'merge';

	/**
	 * Merge two or more objects recursively 
	 * @param bool? clone
	 * @param mixed,... arguments
	 * @return object
	 */

	Public.recursive = function(clone) {

		return merge(clone === true, true, arguments);

	};

	/**
	 * Clone the input removing any reference
	 * @param mixed input
	 * @return mixed
	 */

	Public.clone = function(input) {

		var output = input,
			type = typeOf(input),
			index, size;

		if (type === 'array') {

			output = [];
			size = input.length;

			for (index=0;index<size;++index)

				output[index] = Public.clone(input[index]);

		} else if (type === 'object') {

			output = {};

			for (index in input)

				output[index] = Public.clone(input[index]);

		}

		return output;

	};

	/**
	 * Merge two objects recursively
	 * @param mixed input
	 * @param mixed extend
	 * @return mixed
	 */

	function merge_recursive(base, extend) {

		if (typeOf(base) !== 'object')

			return extend;

		for (var key in extend) {

			if (typeOf(base[key]) === 'object' && typeOf(extend[key]) === 'object') {

				base[key] = merge_recursive(base[key], extend[key]);

			} else {

				base[key] = extend[key];

			}

		}

		return base;

	}

	/**
	 * Merge two or more objects
	 * @param bool clone
	 * @param bool recursive
	 * @param array argv
	 * @return object
	 */

	function merge(clone, recursive, argv) {

		var result = argv[0],
			size = argv.length;

		if (clone || typeOf(result) !== 'object')

			result = {};

		for (var index=0;index<size;++index) {

			var item = argv[index],

				type = typeOf(item);

			if (type !== 'object') continue;

			for (var key in item) {

				var sitem = clone ? Public.clone(item[key]) : item[key];

				if (recursive) {

					result[key] = merge_recursive(result[key], sitem);

				} else {

					result[key] = sitem;

				}

			}

		}

		return result;

	}

	/**
	 * Get type of variable
	 * @param mixed input
	 * @return string
	 *
	 * @see http://jsperf.com/typeofvar
	 */

	function typeOf(input) {

		return ({}).toString.call(input).slice(8, -1).toLowerCase();

	}

	if (isNode) {

		module.exports = Public;

	} else {

		window[publicName] = Public;

	}

})(typeof module === 'object' && module && typeof module.exports === 'object' && module.exports);
},{}],19:[function(require,module,exports){
module.exports={
  "name": "rtcninja",
  "version": "0.6.4",
  "description": "WebRTC API wrapper to deal with different browsers",
  "author": {
    "name": "Iñaki Baz Castillo",
    "email": "inaki.baz@eface2face.com",
    "url": "http://eface2face.com"
  },
  "contributors": [
    {
      "name": "Jesús Pérez",
      "email": "jesus.perez@eface2face.com"
    }
  ],
  "license": "MIT",
  "main": "lib/rtcninja.js",
  "homepage": "https://github.com/eface2face/rtcninja.js",
  "repository": {
    "type": "git",
    "url": "https://github.com/eface2face/rtcninja.js.git"
  },
  "keywords": [
    "webrtc"
  ],
  "engines": {
    "node": ">=0.10.32"
  },
  "dependencies": {
    "bowser": "^1.0.0",
    "debug": "^2.2.0",
    "merge": "^1.2.0"
  },
  "devDependencies": {
    "browserify": "^11.0.1",
    "gulp": "git+https://github.com/gulpjs/gulp.git#4.0",
    "gulp-expect-file": "0.0.7",
    "gulp-filelog": "^0.4.1",
    "gulp-header": "^1.7.1",
    "gulp-jscs": "^2.0.0",
    "gulp-jscs-stylish": "^1.1.2",
    "gulp-jshint": "^1.11.2",
    "gulp-rename": "^1.2.2",
    "gulp-uglify": "^1.4.0",
    "jshint-stylish": "^2.0.1",
    "retire": "^1.1.1",
    "shelljs": "^0.5.3",
    "vinyl-source-stream": "^1.1.0"
  },
  "gitHead": "18789cbefdb5a6c6c038ab4f1ce8e9e3813135b0",
  "bugs": {
    "url": "https://github.com/eface2face/rtcninja.js/issues"
  },
  "_id": "rtcninja@0.6.4",
  "scripts": {},
  "_shasum": "7ede8577ce978cb431772d877967c53aadeb5e99",
  "_from": "rtcninja@0.6.4",
  "_npmVersion": "2.5.1",
  "_nodeVersion": "0.12.0",
  "_npmUser": {
    "name": "ibc",
    "email": "ibc@aliax.net"
  },
  "dist": {
    "shasum": "7ede8577ce978cb431772d877967c53aadeb5e99",
    "tarball": "http://registry.npmjs.org/rtcninja/-/rtcninja-0.6.4.tgz"
  },
  "maintainers": [
    {
      "name": "ibc",
      "email": "ibc@aliax.net"
    }
  ],
  "directories": {},
  "_resolved": "https://registry.npmjs.org/rtcninja/-/rtcninja-0.6.4.tgz"
}

},{}],20:[function(require,module,exports){
var _global = (function() { return this; })();
var nativeWebSocket = _global.WebSocket || _global.MozWebSocket;


/**
 * Expose a W3C WebSocket class with just one or two arguments.
 */
function W3CWebSocket(uri, protocols) {
	var native_instance;

	if (protocols) {
		native_instance = new nativeWebSocket(uri, protocols);
	}
	else {
		native_instance = new nativeWebSocket(uri);
	}

	/**
	 * 'native_instance' is an instance of nativeWebSocket (the browser's WebSocket
	 * class). Since it is an Object it will be returned as it is when creating an
	 * instance of W3CWebSocket via 'new W3CWebSocket()'.
	 *
	 * ECMAScript 5: http://bclary.com/2004/11/07/#a-13.2.2
	 */
	return native_instance;
}


/**
 * Module exports.
 */
module.exports = {
    'w3cwebsocket' : nativeWebSocket ? W3CWebSocket : null,
    'version'      : require('./version')
};

},{"./version":21}],21:[function(require,module,exports){
module.exports = require('../package.json').version;

},{"../package.json":22}],22:[function(require,module,exports){
module.exports={
  "name": "websocket",
  "description": "Websocket Client & Server Library implementing the WebSocket protocol as specified in RFC 6455.",
  "keywords": [
    "websocket",
    "websockets",
    "socket",
    "networking",
    "comet",
    "push",
    "RFC-6455",
    "realtime",
    "server",
    "client"
  ],
  "author": {
    "name": "Brian McKelvey",
    "email": "brian@worlize.com",
    "url": "https://www.worlize.com/"
  },
  "contributors": [
    {
      "name": "Iñaki Baz Castillo",
      "email": "ibc@aliax.net",
      "url": "http://dev.sipdoc.net"
    }
  ],
  "version": "1.0.21",
  "repository": {
    "type": "git",
    "url": "git+https://github.com/theturtle32/WebSocket-Node.git"
  },
  "homepage": "https://github.com/theturtle32/WebSocket-Node",
  "engines": {
    "node": ">=0.8.0"
  },
  "dependencies": {
    "debug": "~2.2.0",
    "nan": "~1.8.x",
    "typedarray-to-buffer": "~3.0.3",
    "yaeti": "~0.0.4"
  },
  "devDependencies": {
    "buffer-equal": "^0.0.1",
    "faucet": "^0.0.1",
    "gulp": "git+https://github.com/gulpjs/gulp.git#4.0",
    "gulp-jshint": "^1.11.2",
    "jshint-stylish": "^1.0.2",
    "tape": "^4.0.1"
  },
  "config": {
    "verbose": false
  },
  "scripts": {
    "install": "(node-gyp rebuild 2> builderror.log) || (exit 0)",
    "test": "faucet test/unit",
    "gulp": "gulp"
  },
  "main": "index",
  "directories": {
    "lib": "./lib"
  },
  "browser": "lib/browser.js",
  "license": "Apache-2.0",
  "gitHead": "8f5d5f3ef3d946324fe016d525893546ff6500e1",
  "bugs": {
    "url": "https://github.com/theturtle32/WebSocket-Node/issues"
  },
  "_id": "websocket@1.0.21",
  "_shasum": "f51f0a96ed19629af39922470ab591907f1c5bd9",
  "_from": "websocket@1.0.21",
  "_npmVersion": "2.12.1",
  "_nodeVersion": "2.3.4",
  "_npmUser": {
    "name": "theturtle32",
    "email": "brian@worlize.com"
  },
  "maintainers": [
    {
      "name": "theturtle32",
      "email": "brian@worlize.com"
    }
  ],
  "dist": {
    "shasum": "f51f0a96ed19629af39922470ab591907f1c5bd9",
    "tarball": "http://registry.npmjs.org/websocket/-/websocket-1.0.21.tgz"
  },
  "_resolved": "https://registry.npmjs.org/websocket/-/websocket-1.0.21.tgz",
  "readme": "ERROR: No README data found!"
}

},{}]},{},[4])(4)
});


//# sourceMappingURL=sylkrtc.js.map