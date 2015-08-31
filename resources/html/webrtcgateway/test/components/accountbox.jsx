
var AccountBox = React.createClass({
    getInitialState: function() {
        return {accountId: '',
                password: '',
                account: null,
                registrationState: null,
                addInProgress: false,
                registerInProgress: false,
        };
    },

    componentWillReceiveProps: function(nextProps) {
        if (this.props.connection !== null && nextProps.connection === null) {
            // we have been disconnected!
            this.replaceState(this.getInitialState());
        }
    },

    handleAccountIdChange: function(event) {
        this.setState({accountId: event.target.value});
    },

    handlePasswordChange: function(event) {
        this.setState({password: event.target.value});
    },

    registrationStateChanged: function(oldState, newState, data) {
        this.setState({registrationState: newState});
        switch (newState) {
            case 'failed':
            case 'registered':
            case 'unregistered':
            case null:
                this.setState({registerInProgress: false});
                break;
            default:
                break;
        }
    },

    toggleAdd: function(event) {
        event.preventDefault();
        var conn = this.props.connection;
        var self = this;
        this.setState({addInProgress: true});
        if (this.state.account === null) {
            var options = {account: this.state.accountId, password: this.state.password};
            conn.addAccount(options, function(error, account) {
                if (!error) {
                    account.on('registrationStateChanged', self.registrationStateChanged);
                } else {
                    console.log('Error adding account: ' + error);
                }
                self.setState({account: account, addInProgress: false});
                self.props.setAccount(account);
            });
        } else {
            conn.removeAccount(this.state.account, function(error) {
                if (error) {
                    console.log('Error removing account: ' + error);
                }
                self.replaceState(self.getInitialState());
                self.props.setAccount(null);
            });
        }
    },

    toggleRegister: function(event) {
        event.preventDefault();
        this.setState({registerInProgress: true});
        if (this.state.registrationState !== null) {
            this.state.account.unregister();
        } else {
            this.state.account.register();
        }
    },

    render: function() {
        var connectionReady = this.props.connection !== null && this.props.connection.state === 'ready';
        var accountReady = this.state.account !== null;
        return (
            <div className="account-box" style={{overflow: 'hidden'}}>
                <form role="form">
                    <div className="input-group">
                        <span className="input-group-addon" id="account-input" style={{minWidth: '100px', textAlign: 'left'}}>Account</span>
                        <input type="text" aria-describedby="account-input" className="form-control" disabled={!connectionReady || accountReady} value={this.state.accountId} placeholder="Enter SIP URI: user@domain" onChange={this.handleAccountIdChange}/>
                    </div>
                    <br/>
                    <div className="input-group">
                        <span className="input-group-addon" id="account-password-input" style={{minWidth: '100px', textAalign: 'left'}}>Password</span>
                        <input type="password" aria-describedby="account-password-input" className="form-control" disabled={!connectionReady || accountReady} value={this.state.password} onChange={this.handlePasswordChange}/>
                    </div>
                    <br/>
                    <div className="btn-group pull-right">
                        <button type="submit" className="btn btn-primary" disabled={this.state.accountId.length === 0 || this.state.password.length === 0 || this.state.addInProgress} onClick={this.toggleAdd}>
                            {this.state.account !== null ? "Remove" : "Add"}
                        </button>
                        <button type="button" className="btn btn-default" disabled={!accountReady || this.state.registerInProgress} onClick={this.toggleRegister}>
                            {this.state.registrationState !== null ? "Unregister" : "Register"}
                        </button>
                    </div>
                </form>
            </div>
        );
    },
});
