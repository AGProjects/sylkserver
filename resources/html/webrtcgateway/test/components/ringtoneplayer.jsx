
var RingtonePlayer = React.createClass({
    audioEnded: function() {
        this.timeout = setTimeout(() => {
            this.refs.audio.getDOMNode().play();
        }, 4500);
    },

    componentDidMount: function() {
        this.refs.audio.getDOMNode().addEventListener('ended', this.audioEnded);
    },

    componentWillUnmount: function() {
        if (this.timeout) {
            clearTimeout(this.timeout);
        }
        this.timeout = null;
        this.refs.audio.getDOMNode().removeEventListener('ended', this.audioEnded);
    },

    render: function() {
        return (
            <div>
                <audio autoPlay ref='audio'>
                    <source src='sounds/ringtone.wav' type="audio/wav"/>
                </audio>
            </div>
        );
    }
});
