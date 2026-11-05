# Janus patches

SylkServer's WebRTC gateway runs Debian's Janus 1.1.2 package (bookworm),
rebuilt with the patches in this directory. They apply with `patch -p1`, in
the order of their numbers, after Debian's own patches. Each fix is also
proposed upstream; a patch is dropped once Janus and Debian have it.

## 001-nosip-per-media-srtp.patch

NoSIP plugin (`src/plugins/janus_nosip.c`): decide SDES-SRTP per media line.

NoSIP decided whether to decrypt incoming RTP/RTCP with one session-wide flag
(`has_srtp_remote`) and whether to encrypt outgoing packets with another
(`has_srtp_local`), while the SRTP contexts are per media line. Two bugs
followed:

1. **Crash with a peer that mixes SRTP and RTP.** When an optional SDES offer
   (`"srtp": "sdes_optional"`) is answered with `a=crypto` on the audio line
   only, the video line has no SRTP context. The first RTP or RTCP packet the
   peer sends on it went to `srtp_unprotect()` with a NULL context: Janus died
   with SIGSEGV, dropping every client of the gateway. Outgoing packets on that
   line were also SRTP protected, which the peer cannot decode.

   Fix: `janus_nosip_srtp_active()`: a line uses SRTP only when its contexts
   exist in both directions. Used for incoming and outgoing RTP and RTCP and
   in the relay thread. `janus_nosip_srtp_plain_line()`: no `a=crypto` is added
   to a line the peer uses as plain RTP while it uses SDES on others (answers
   and later re-offers).

2. **No crypto when video is added to an SDES audio call.** In a re-offer
   adding video, the new video line has no remote SRTP context yet, only
   because the peer has not answered it. It was taken for a plain line and
   offered without `a=crypto`.

   Fix: a line counts as plain only once the peer has negotiated it (it has a
   remote port); a new line gets crypto like the others.

Lines where both sides use SDES behave as before. Only
`libjanus_nosip.so` changes.

Upstream: `docs/janus/PR.md` and the patch for Janus master,
`docs/janus/0001-NoSIP-decide-SDES-SRTP-per-media-line-fix-crash-on-m.patch`
(it has one more hunk, for the PLI sent to the peer, code that Janus 1.1.2
does not have). Reproduction: `docs/janus/repro_nosip_mixed_srtp.py`;
background on the SDP answers: `docs/janus/rtcp-fb-answer.md`.

## Adding a patch

Name it `NNN-short-description.patch` with the next number, made with
`git diff` / `git format-patch` against the Janus source (a/ b/ paths, `-p1`),
and describe it in this file. It must apply without fuzz on top of the earlier
patches (step 1 below checks that).

## Building the Debian package

On a bookworm machine with devscripts and quilt (`sudo apt install devscripts quilt`):

    cd ~/work
    dget -u https://deb.debian.org/debian/pool/main/j/janus/janus_1.1.2-1.dsc
    cd janus-1.1.2
    for patch in ~/work/sylkserver/patches/janus/[0-9]*.patch; do
        cp $patch debian/patches/ag-$(basename $patch)
        echo ag-$(basename $patch) >> debian/patches/series
    done
    dch -v 1.1.2-1+ag2 -D bookworm "AG Projects patches: $(cd ~/work/sylkserver/patches/janus; echo [0-9]*.patch)"

dget leaves Debian's patches applied; apply ours after them, as the build will
(it fails here if one does not apply):

    dpkg-source --before-build .                 # ... applying ag-001-nosip-per-media-srtp.patch
    head -1 debian/changelog                     # janus (1.1.2-1+ag2) bookworm; urgency=medium

`janus_1.1.2.orig.tar.gz` must stay next to `janus-1.1.2/`: the package is
`3.0 (quilt)` and the build needs the upstream tarball.

The `+agN` version sorts above Debian's `1.1.2-1`, so apt prefers it. Use a
higher N every time the patch set changes (`1.1.2-1+ag1` had only the first
NoSIP fix). If Debian releases a newer revision, start again from its .dsc
with `+ag1`, or `apt-mark hold janus` on the server.

Build, on bookworm, against bookworm's libraries (the build dependencies
include doxygen and the node/rollup toolchain: the first build takes a while):

    sudo apt build-dep ./
    dpkg-buildpackage -us -uc -b

## Installing and checking

    sudo dpkg -i ../janus_1.1.2-1+ag2_amd64.deb   # or from your package repository
    sudo systemctl restart janus
    dpkg -l janus                                 # 1.1.2-1+ag2

`docs/janus/repro_nosip_mixed_srtp.py` against a test instance with the HTTP
API enabled: "Janus died" before, "Janus is alive" after. In production, a
sylk-session video call to 3333 (answered with opus, see
`docs/janus/rtcp-fb-answer.md`) no longer logs
`janus.service: Main process exited, code=dumped, status=11/SEGV`.

## 002-nosip-srtp-rekey.patch

NoSIP plugin (`src/plugins/janus_nosip.c`): use the new SDES key when the SIP
peer changes it on a re-INVITE.

When the remote party removes a media line and adds it again (video turned
off and on in Blink), the line comes back as a new stream with a new
`a=crypto` key and port. NoSIP took the new port but kept the first inbound
SRTP context ("Remote SRTP is already set"), so every packet from the peer
failed authentication and was dropped: the WebRTC client got no video while
the peer was sending it.

Fix: on an update, an `a=crypto` key different from the one in use is queued
(`*_srtp_pending_*`) and the relay thread is woken through the existing
`updated` pipe; it replaces the inbound context itself
(`janus_nosip_srtp_apply_pending()`), as it is the only user of that context,
so the swap never races `srtp_unprotect()`. Our own (outbound) key does not
change. Only `libjanus_nosip.so` changes.
