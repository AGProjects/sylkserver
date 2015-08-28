
var IncomingCall = React.createClass({
    answerClicked: function(event) {
        event.preventDefault();
        var callOptions = {pcConfig: {iceServers: [{urls: 'stun:stun.l.google.com:19302'}]}};
        this.props.call.answer(callOptions);
    },

    hangupClicked: function(event) {
        event.preventDefault();
        this.props.call.terminate();
    },

    render: function() {
        return (
            <div className="call-box" style={{overflow: 'hidden'}}>
                <form role="form">
                    <div className="input-group">
                        <input type="text" id="target-input" className="form-control" disabled value={"Incoming call from " + this.props.call.remoteIdentity}/>
                        <span className="input-group-btn">
                            <button type="submit" className="btn btn-success" onClick={this.answerClicked}>
                                Answer
                            </button>
                            <button type="submit" className="btn btn-danger" onClick={this.hangupClicked}>
                                Hangup
                            </button>
                        </span>
                    </div>
                </form>
            </div>
        );
    },
});
