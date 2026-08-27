# Third-party notices

This repository does not vendor third-party executable binaries. During an
installation it obtains software from the following upstream sources.

## MediaMTX

- Project: <https://github.com/bluenviron/mediamtx>
- Pinned release: v1.20.1 for linux/amd64
- License: MIT
- Integrity: the release archive is checked against the SHA-256 value embedded
  in `scripts/install_runtime.sh` before extraction.
- Download fallback: when GitHub's release CDN is unreachable, the installer
  derives GitHub's official `releaseassetproduction.blob.core.windows.net`
  storage URL from a fresh signed GitHub redirect. It is not a third-party
  mirror, and the same pinned SHA-256 check remains mandatory.

## FFmpeg

- Project: <https://ffmpeg.org/>
- Package source: signed Ubuntu 24.04 LTS repositories
- Validated package family: `7:6.1.1-3ubuntu5` and official Ubuntu Pro
  `+esmN` successors
- License: FFmpeg is primarily LGPL; the effective license of an Ubuntu build
  can include GPL terms depending on enabled components. Consult
  `/usr/share/doc/ffmpeg/copyright` on the installed server.

The Noble base package is in Ubuntu's Universe component. Operators who use
Ubuntu Pro may receive an ESM build. The installer never downgrades a newer ESM
build and does not hold FFmpeg against future security updates; every candidate
is restricted to the validated official Noble version family.

## Python

- Runtime: Ubuntu's `python3` package
- License: Python Software Foundation License

All names and trademarks belong to their respective owners.
