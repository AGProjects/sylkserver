
SylkServer
----------

A State of the art, extensible RTC Application Server.

Home page: http://sylkserver.com


License
-------

SylkServer is licensed under GNU General Public License version 3. A copy of
the license is available at http://www.fsf.org/licensing/licenses/gpl-3.0.html


Description
-----------

SylkServer allows creation and delivery of rich multimedia applications
accessed by WebRTC applications, SIP clients and XMPP endpoints.  The server
supports SIP and XMPP signaling, RTP, MSRP and WebRTC media planes, has
built in capabilities for creating multiparty conferences with Audio and
Video, IM/ File Transfers and can be extended with custom applications by
using Python language.


Deployment scenarios
--------------------

For SIP functionality, SylkServer is typically deployed behind a SIP Proxy
that is designed to route the inbound and outbound traffic, handle the
authentication, authorization and accounting.

SylkServer can be deployed as a standalone conference server on a private
network to serve SIP clients on the same LAN by using bonjour mode.  Blink
for MacOSX can be used for automatic discovery of SylkServer instances in
their neighborhood.

SylkServer can be deployed as a standalone WebRTC video conference server. 
The client side can be a standalone application (like the companion Sylk
client) and modern web browsers with WebRTC support (like Chrome, Firefox,
Safari and Edge browsers).


Features
--------

SIP Signaling

 - TLS, TCP and UDP transports
 - INVITE and REFER
 - SUBSCRIBE/NOTIFY
 - Bonjour mode

NAT Traversal

 - SIP Outbound
 - ICE clients
 - MSRP Relay clients
 - MSRP ACM clients

Audio

 - Wideband (Opus, G722 and Speex)
 - Narrowband (G711 and GSM)
 - SRTP encryption (SDES and ZRTP key-exchanges)
 - Hold/Unhold
 - RTP timeout
 - DTMF handling

Video

 - H.264, VP8 and VP9 codecs
 - SRTP encryption (SDES and ZRTP key-exchange)

Instant Messaging

 - MSRP protocol
 - CPIM envelope
 - Is-composing
 - Delivery reports

File Transfer

 - MSRP protocol
 - Progress reports
 - Conference-info extension
 - Conference room persistent

Audio/Chat conferencing

 - Wideband RTP mixer
 - MSRP switch
 - XMPP MUC
 - Multiparty screensharing
 - Conference event package

Video conferencing

 - WebRTC media
 - Encryption (TLS, sRTP)
 - VP8/VP9/H.264 video codecs
 - Opus wideband audio
 - SFU scaling methodology
 - Floor control

XMPP Gateway

 - Server to Server mode
 - IM (MSRP sessions and SIP Messages)
 - Presence (SIMPLE and XMPP)

P2P WebRTC Gateway

See README.webrtc file.


Applications
------------

SIP applications

When a SIP request arrives at SylkServer, an application can be selected
depending on the SIP Request URI.  The selection mechanism is described in
detail in the sample configuration file config.ini.sample.  SIP requests can
be bridged to WebRTC aplications or XMPP endpoints using the applications
described below.


SIP conference

SylkServer allows SIP end-points to create ad-hoc conference rooms by
sending INVITE to a random username at the hostname or domain where the
server runs.  Other participants can then join by sending an INVITE to the
same SIP URI used to create the room.  The INVITE and subsequent re-INVITE
methods may contain one or more media types supported by the server.  Each
conference room mixed audio, instant messages and uploded files are
dispatched to all participants.  One can remove or add participants by
sending a REFER method to the conference URI.

If a participant sends a file to the SIP URI of the room, the server will
accept it, store it for the duration of the conference and offer it to all
participants either present at that moment, or offer it on demand to those
that have joined the conference at a later moment.

Using an extension to MSRP chat protocol, the server provides also
multi-party screen sharing capability.


XMPP Gateway

SylkServer can act as a transparent inter-domain gateway between SIP and
XMPP protocols.  This can be used by a SIP service provider to bridge out to
external XMPP domains or to receive incoming chat messages and Jingle audio
sessions from remote XMPP domains to its local SIP users.  In a similar
fashion, a XMPP service provider can use the gateway to bridge out to
external SIP domains and handle incoming chat requests from SIP domains to
the XMPP users it serves.

A media session or a presence session initiated by an incoming connection on
the XMPP side is translated into an outgoing request on the SIP side and the
other way around.  To make this possible, proper SIP or XMPP records must
exists into the DNS zone for the domain that needs the gateway service.


WebRTC gateway

Used to bridge audio and video calls between SIP clients and WebRTC
applications.  A simple to use client API (sylkrtc.js) is provided for
developing web pages that include such functionality.  This application
supports transparently any audio/video codec negotiated by the end-points,
however WebRTC has standardised particular codecs for the use on the web
(like OPUS for audio), therefore the SIP clients must support the same set
of codecs.

See https://webrtc.sipthor.net for a working example.


WebRTC multi-party conference

This application allows WebRTC enabled end-points to organise ad-hoc
multi-media conferences with audio/video, chat, file and screen sharing.

For audio and video, SylkServer implements Selective Forwarding Unit (SFU)
functionality by using Janus backend.  SFUs use little resources on the
server side, allowing for handling much more load than classic MCUs.

For text chat, the media is mixed inside the SIP conference application,
which makes it interoperable with SIP MSRP chat and XMPP protocols and
standards.

Sylk WebRTC client is provided as a sample client and new web clients can be
developed using sylkrtc API.

The bandwidth usage is optimised in such a way that independent of the
number of participants present in the conference, the bandwidth required by
each participant is not greater than for a direct video call between only
two participants.

For scalling up beyond one server instance, AG Projects provides a
commercial product called SIP Thor.


Standards
---------

The server implements relevant features from the following standards:

 - SIP (RFC3261, RFC3263) and related RFCs for DNS, ICE and RTP
 - MSRP protocol RFC4975
 - MSRP relay extension RFC4976
 - MSRP File Transfer RFC5547
 - MSRP switch RFC7701
 - MSRP Alternative Connection Model RFC6135
 - Indication of Message Composition RFC3994
 - CPIM Message Format RFC3862
 - Instant Message Disposition Notification (IMDN) RFC5438
 - Conference event package RFC4575
 - A Framework for Conferencing with SIP RFC4353
 - Conferencing for User Agents RFC4579
 - Conferencing for User Agents RFC4579
   5.1  INVITE: Joining a Conference Using the Conference URI - Dial-In
   5.2  INVITE: Adding a Participant by the Focus - Dial-Out
   5.5  REFER: Requesting a Focus to Add a New Resource to a Conference
   5.11 REFER with BYE: Requesting a Focus to Remove a Participant from a Conference
 - XMPP core (RFC 6120) http://xmpp.org/rfcs/rfc6120.html
 - XMPP extensions http://xmpp.org/xmpp-protocols/xmpp-extensions
 - Instant Messaging and Presence http://xmpp.org/rfcs/rfc6121.html
 - Interworking between the Session Initiation Protocol (SIP) and the
   Extensible Messaging and Presence Protocol (XMPP):
     - Presence: RFC7248
     - IM: RFC7572
     - Chat: RFC7573
     - Multi-party chat: RFC7702
 - WebRTC standards http://www.w3.org/TR/webrtc/
 - RTP Topologies RFC7667
   3.7 Selective Forwarding Middlebox
 - OMA RCC0.7 Filetransfer over HTTP/POST


Support
-------

The project is developed and supported by AG Projects. The support is
provided on a best-effort basis. "best-effort" means that we try to solve
the bugs you report or help fix your problems as soon as we can, subject to
available resources.

To request support you must the use SIP Beyond VoIP mailing list:

http://lists.ag-projects.com/mailman/listinfo/sipbeyondvoip

For commercial support contact AG Projects http://ag-projects.com


Credits
-------

Special thanks to our sponsors:

 - NLnet Foundation http://nlnet.nl
 - SIDN Fonds https://sidnfonds.nl
 - ISOC Nederland http://isoc.nl


Developers
----------

 - Dan Pascu
 - Tijmen de Mes
 - Saul Ibarra Corretge
 - Adrian Georgescu

