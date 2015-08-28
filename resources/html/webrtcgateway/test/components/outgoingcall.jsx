
var OutgoingCall = React.createClass({
    getInitialState: function() {
        return {
            call: null,
            targetUri: '',
        };
    },

    callStateChanged: function(oldState, newState, data) {
        if (!this.isMounted()) {
            // we might get here when the component has been unmounted
            return;
        }
        if (newState === 'terminated') {
            this.replaceState(this.getInitialState());
        }
    },

    toggleCall: function(event) {
        event.preventDefault();
        if (this.state.call === null) {
            var callOptions = {pcConfig: {iceServers: [{urls: 'stun:stun.l.google.com:19302'}]}};
            var call = this.props.account.call(this.state.targetUri, callOptions);
            call.on('stateChanged', this.callStateChanged);
            this.setState({call: call});
        } else {
            this.state.call.terminate();
        }
    },

    handleTargetChange: function(event) {
        this.setState({targetUri: event.target.value});
    },

    render: function() {
        var ready = this.props.account !== null;
        var buttonClasses = 'btn';
        if (this.state.call) {
            buttonClasses += ' btn-danger';
        } else {
            buttonClasses += ' btn-success';
        }
        return (
            <div className="call-box" style={{overflow: 'hidden'}}>
                <form role="form">
                    <div className="input-group">
                        <input type="text" id="target-input" className="form-control" disabled={!ready || this.state.call !== null} onChange={this.handleTargetChange} value={this.state.targetUri} placeholder="Call SIP URI (user@domain format)"/>
                        <span className="input-group-btn">
                            <button type="submit" className={buttonClasses} onClick={this.toggleCall} disabled={!ready || this.state.targetUri.length === 0}>
                                {this.state.call ? "Hangup": "Call"}
                            </button>
                        </span>
                    </div>
                </form>
            </div>
        );
    },
});
