# Third-party notices

## karpathy-guidelines

- Bundled file: `ductor_bot/_home_defaults/workspace/skills/karpathy-guidelines/SKILL.md`
- Source: [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills)
- Copyright holder: forrestchang, identified as the author in the source project's `.claude-plugin/plugin.json`.
- License: MIT, as declared by the source project and the bundled skill.
- Bundled notice: ductor_bot/_home_defaults/workspace/skills/karpathy-guidelines/NOTICE

The guidelines are derived from Andrej Karpathy's observations on coding pitfalls.

## skill-creator

- Bundled directory: `ductor_bot/_home_defaults/workspace/skills/skill-creator/`
- License: Apache-2.0.
- Bundled license: [LICENSE.txt](ductor_bot/_home_defaults/workspace/skills/skill-creator/LICENSE.txt).

## Web fonts

- Pretendard Variable: [orioncactus/pretendard](https://github.com/orioncactus/pretendard).
  Copyright 2021 Kil Hyung-jin. SIL Open Font License 1.1.
  Bundled license: [LICENSE-Pretendard.txt](ductor_bot/webchat/web/static/fonts/LICENSE-Pretendard.txt).
- JetBrains Mono: [JetBrains/JetBrainsMono](https://github.com/JetBrains/JetBrainsMono).
  Copyright 2020 The JetBrains Mono Project Authors. SIL Open Font License 1.1.
  Bundled license: [LICENSE-JetBrainsMono.txt](ductor_bot/webchat/web/static/fonts/LICENSE-JetBrainsMono.txt).

## Three.js

- The bundled Starflow background embeds Three.js 0.180.0.
- Source: [Three.js](https://github.com/mrdoob/three.js/tree/r180).
- Copyright 2010-2025 three.js authors. MIT license.
- Bundled license: [LICENSE-Three.js.txt](ductor_bot/webchat/web/static/starflow/LICENSE-Three.js.txt).

## Segno

- Dependency used to render TOTP enrollment QR codes: [heuer/segno](https://github.com/heuer/segno).
- License: BSD-3-Clause; [upstream license](https://github.com/heuer/segno/blob/master/LICENSE).

Copyright (c) 2016 - 2025, Lars Heuer. All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

## NPMplus gateway template

- Derived file: `docker/webchat/npmplus/_common.conf`, adapted to preserve ACME
  HTTP-01 challenges before forced HTTPS redirects.
- Source: [ZoeyVid/NPMplus](https://github.com/ZoeyVid/NPMplus),
  `/app/templates/_common.conf` in the pinned gateway image.
- License: MIT; [upstream license](https://github.com/ZoeyVid/NPMplus/blob/develop/MIT.LICENSE).
- The original image template SHA-256 is verified by `scripts/webchat.py` before startup.
- Copied file: `scripts/tests/fixtures/npmplus-nginx.conf`, the pinned image's
  `/usr/local/nginx/conf/nginx.conf`, used to test the trusted-proxy patch that
  `scripts/webchat.py` applies after verifying the original SHA-256.

MIT License

Copyright (c) 2017

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
