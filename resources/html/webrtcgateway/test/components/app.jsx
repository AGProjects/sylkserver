
var SylkTest = React.createClass({
    getInitialState: function() {
        return {
            connection: null,
            connectionState: null,
            account: null,
            registrationState: null,
            currentCall: null,
            callState: null,
        };
    },

    getServerUrl: function() {
        return window.location.protocol.replace('http', 'ws') + '//' + window.location.host + '/webrtcgateway/ws';
    },

    connectionStateChanged: function(oldState, newState) {
        switch (newState) {
            case 'closed':
                this.setState({connection: null, connectionState: newState});
                break;
            case 'disconnected':
                if (this.state.currentCall) {
                    this.state.currentCall.terminate();
                }
                this.setState({account: null, registrationState: null, currentCall: null, callState: null, connectionState: newState});
                break;
            default:
                this.setState({connectionState: newState});
                break;
        }
    },

    registrationStateChanged: function(oldState, newState, data) {
        var state = newState;
        if (newState === 'failed') {
            state += ' (' + data.reason + ')'
        }
        this.setState({registrationState: state});
    },

    callStateChanged: function(oldState, newState, data) {
        var state = newState;
        var currentCall = this.state.currentCall;
        if (newState === 'terminated') {
            state += ' (' + data.reason + ')';
            currentCall = null;
            setTimeout(() => {
                if (this.state.currentCall === null) {
                    this.setState({callState: null});
                }
            }, 4000);
        }
        this.setState({currentCall: currentCall, callState: state});
    },

    setAccount: function(account) {
        if (account !== null) {
            account.on('registrationStateChanged', this.registrationStateChanged);
            account.on('incomingCall', this.handleNewCall);
            account.on('outgoingCall', this.handleNewCall);
        }
        this.setState({account: account});
    },

    handleNewCall: function(call) {
        if (this.state.currentCall !== null) {
            call.terminate();
        } else {
            call.on('stateChanged', this.callStateChanged);
            this.setState({currentCall: call, callState: call.state});
        }
    },

    toggleConnect: function(event) {
        event.preventDefault();
        if (this.state.connection === null) {
            var connection = sylkrtc.createConnection({server: this.getServerUrl()});
            connection.on('stateChanged', this.connectionStateChanged);
            this.setState({connection: connection});
        } else {
            this.state.connection.close();
        }
    },

    render: function() {
        var accountBox;
        var callBox;
        var videoBox;
        if (sylkrtc.isWebRTCSupported()) {
            accountBox = <AccountBox connection={this.state.connectionState === 'ready' ? this.state.connection : null} setAccount={this.setAccount}/>;
            if (this.state.callState === 'established') {
                videoBox = <VideoBox call={this.state.currentCall}/>
            } else {
                if (this.state.currentCall === null || this.state.currentCall.direction === 'outgoing') {
                    callBox = <OutgoingCall account={this.state.account}/>
                } else {
                    callBox = <IncomingCall call={this.state.currentCall}/>
                }
            }
        } else {
            accountBox = <div>
                            <h2>Your browser does not support WebRTC</h2>
                            <p>
                                Too bad! Checkout the WebRTC support across browsers in the link below.
                                <br/>
                                <a href="http://iswebrtcreadyyet.com/">Is WebRTC ready yet?</a>
                            </p>
                        </div>;
        }
        return (
            <div>
                <nav className="navbar navbar-inverse">
                    <div className="container">
                        <div className="navbar-header">
                            <a href="#" className="navbar-brand">SylkServer WebRTC Test App</a>
                        </div>
                        <button type="button" className="btn btn-primary navbar-btn pull-right" onClick={this.toggleConnect}>
                            {this.state.connection ? "Disconnect" : "Connect"}
                        </button>
                    </div>
                </nav>
                <div className="container">
                    <br/>
                    <div className="panel panel-default">
                        <div className="panel-body">
                            {accountBox}
                            <hr/>
                            {videoBox}
                            {callBox}
                            <br/>
                            <br/>
                        </div>
                        <div className="panel-footer">
                            WebSocket server &#8212; {this.getServerUrl()}
                        </div>
                    </div>
                </div>
                <nav className="navbar navbar-inverse navbar-fixed-bottom">
                    <div className="container">
                        <p className="navbar-text">
                            <span className="label label-default">Connection</span>
                            &nbsp;
                            <span className="label label-info">{this.state.connectionState || "disconnected"}</span>
                        </p>
                        <p className="navbar-text">
                            <span className="label label-default">Registration</span>
                            &nbsp;
                            <span className="label label-info"> {this.state.registrationState || "unregistered"}</span>
                        </p>
                        <p className="navbar-text">
                            <span className="label label-default">Call status</span>
                            &nbsp;
                            <span className="label label-info">{this.state.callState || "idle"}</span>
                        </p>
                    </div>
                </nav>
            </div>
        );
    }
});
