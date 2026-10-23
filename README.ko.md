한국어 · [English](README.md)

# DT Agent

DT Agent는 [PleasePrompto/ductor](https://github.com/PleasePrompto/ductor)의 핵심 동작을 기반으로 합니다. 개인적인 환경과 취향에 맞춘 변경점이 너무 많이 쌓여 별도 저장소로 분리했습니다. 이 변경점이 마음에 들면 DT Agent를 사용하고, 그렇지 않거나 잘 모르겠다면 오리지널 ductor부터 써 보시길 권합니다.

오리지널 ductor에서 변경되거나 추가된 코드는 모두 AI가 작성했습니다.

ductor는 프레임워크 차원에서 에이전트의 동작을 차단하는 장치를 최소화한 구조입니다. 그만큼 모델의 판단에 많이 의존하므로, 가급적 고급 모델을 사용할 것을 권합니다.

이 저장소의 제작자는 오랫동안 Claude Opus를 메인 에이전트로, GPT Sol을 서브 에이전트로 사용해 왔고, 상당 부분이 이 조합에 최적화되어 있을 것입니다.

내부 패키지·환경변수·경로·컨테이너 이름(`ductor_bot`, `DUCTOR_HOME`, `~/.ductor`, `ductor-bot` 등)은 원본과의 호환을 위해 그대로 둡니다.

DT Agent는 [github.com/ryuhaneul/dt-agent](https://github.com/ryuhaneul/dt-agent)에서 관리하는 수정 포크이며 upstream 공식 배포가 아닙니다. ductor v0.20.1을 기반으로 합니다. 원저작자는 PleasePrompto(2025–2026)이며 MIT 라이선스를 유지합니다.

Provider CLI는 봇 컨테이너에서, Chromium은 별도 브라우저 컨테이너에서 실행합니다. 위임 라우팅, 중단·복구, 초기 설정, 선택 의존성과 안전한 진단도 변경했습니다. 전체 변경은 [변경점 문서](docs/CHANGES.md)에 정리되어 있습니다.

## 1. 요구 사항과 깨끗한 서버 준비

Linux 서버에 Docker Engine, **Compose 플러그인 v2 이상**, Git, Bash, 호스트 초기 설정용 Python **3.9 이상**과 시간대 데이터베이스가 필요합니다. 봇·개발용 Python은 **3.11 이상**이며 이미지에 설치됩니다. 봇 이미지는 Debian Bookworm/Python 3.11과 Node.js 24, 브라우저 이미지는 Playwright `v1.59.1-noble`을 사용합니다. 이 문서는 Linux x86-64 배포를 기준으로 합니다. 다른 아키텍처·OS는 이미지와 CLI 호환성을 별도로 확인해야 합니다.

이미지 사용자는 초기 설정을 실행한 계정의 UID/GID로 빌드됩니다. 저장소 루트 `.env`의 `DUCTOR_UID`·`DUCTOR_GID`에 저장하며, 키가 없으면 둘 다 1000을 사용합니다. Docker 사용 권한이 있는 일반 로그인 계정을 사용하고, 계정을 바꾸면 이미지를 다시 빌드하십시오. root로 초기 설정하지 마십시오.

최소 RAM·디스크 요구량은 따로 정하지 않았습니다. 두 이미지, 빌드 레이어, workspace·인증·로그 및 백업 공간이 필요합니다. 빌드·재빌드 전에 여유 공간을 확인하십시오.

### Ubuntu 24.04

깨끗한 Ubuntu 24.04 서버에서는 아래 호스트 패키지를 설치하고, [Docker 공식 Ubuntu 설치 안내](https://docs.docker.com/engine/install/ubuntu/)와 [Compose 플러그인 설치 안내](https://docs.docker.com/compose/install/linux/)에 따라 Docker Engine 및 `docker-compose-plugin`을 설치하십시오. 공식 안내의 저장소 키·데몬 설정·사용자 권한 설정까지 초기 설정 전에 완료해야 합니다. 호스트에 맞게 아래 Ubuntu 또는 RHEL 계열 절차를 선택하십시오.

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv tzdata ca-certificates curl
id -u
df -h .
```

Docker가 아직 없으면 위 공식 안내에 따른 아래 Ubuntu APT 저장소 설정으로 Engine·Compose 플러그인을 설치합니다. 지원되는 깨끗한 Ubuntu 호스트에서 실행하고 로그인 사용자의 Docker 권한을 설정한 뒤 재접속하여 검사를 실행하십시오.

```bash
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
UBUNTU_SUITE=$(. /etc/os-release; printf '%s' "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
MACHINE_ARCH=$(dpkg --print-architecture)
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' "$MACHINE_ARCH" "$UBUNTU_SUITE" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
# Reconnect your login session before continuing; Docker access grants host control.
docker version
docker compose version
```

### RHEL 계열: Rocky·Alma·RHEL 8/9/10

[Docker 공식 RHEL 설치 문서](https://docs.docker.com/engine/install/rhel/)의 RPM 저장소 절차를 따릅니다. [Rocky 공식 문서](https://docs.rockylinux.org/gemstones/containers/docker/)도 같은 `linux/rhel/docker-ce.repo`를 사용합니다. 호스트 패키지를 설치하고 `python3`가 3.9 이상인지 확인하십시오. RHEL 계열 Python은 `venv`를 포함하므로 별도 `python3-venv` 패키지는 필요하지 않습니다. 오래된 Python이 기본인 호스트는 배포판의 3.9 이상 Python을 설치하고 이 셸의 `python3`가 그 버전을 실행하게 한 뒤 진행하십시오.

```bash
sudo dnf install git python3 tzdata ca-certificates curl
python3 --version
id -u
df -h .
```

RHEL 계열 8의 기본 Python은 3.9보다 오래될 수 있습니다. [Red Hat은 병렬 설치용 `python39` 패키지를 안내합니다](https://developers.redhat.com/blog/install-python3-rhel). 아래는 시스템 Python을 바꾸지 않고 호스트 스크립트용 환경을 만듭니다. 새 셸에서도 초기 설정이나 다른 호스트 Python 스크립트 실행 전에 다시 활성화하십시오. 초기 설정은 표준 라이브러리를 사용하므로 이 환경에 pip는 필요하지 않습니다.

```bash
# RHEL-family 8: use this if the default python3 is older than 3.9.
sudo dnf install python39
python3.9 -m venv --without-pip "$HOME/.local/share/ductor-host-venv"
source "$HOME/.local/share/ductor-host-venv/bin/activate"
python3 --version
```

아래 제거 명령은 기존 Podman과 runc 사용에도 영향을 줍니다. 해당 워크로드가 있는 서버에서는 영향 확인 후 실행하십시오. Docker 접근 권한을 부여한 뒤 로그인 세션을 다시 연결하고 확인합니다.

```bash
sudo dnf remove docker docker-client docker-client-latest docker-common docker-latest docker-latest-logrotate docker-logrotate docker-engine podman runc
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
# Reconnect your login session before continuing; Docker access grants host control.
docker version
docker compose version
```

Telegram 봇 토큰과 자신의 숫자 Telegram 사용자 ID를 준비하십시오. 그룹에서는 그룹 ID와 사용자 허용 목록을 모두 설정해야 하며 포럼 토픽을 사용하려면 main 봇에 필요한 토픽 권한을 부여하십시오. Claude Code·Codex·Gemini CLI·Grok 중 사용할 provider에 유효한 계정 로그인 또는 지원하는 API 키 인증이 필요합니다. CLI는 이미지가 설치하지만 구독·사용량과 OAuth 인증은 사용자가 준비해야 합니다. 이미지·패키지 다운로드, 메신저 및 provider API에 인터넷 접속이 필요합니다. 선택 기능인 Matrix에는 homeserver, 봇 사용자, 비밀번호 **또는** access token과 device ID, 허용 room·user가 필요합니다. Slack에는 별도 bot·app 토큰과 channel·user 허용 목록이 필요합니다. 현재 초기 설정 도구는 Matrix·Slack을 추가할 때도 Telegram을 요구합니다.

## 2. 클론·초기 설정·빌드·인증·첫 대화

배포 사용자로 저장소를 클론하고 이후 Compose 명령은 저장소 루트에서 실행하십시오.

```bash
git clone https://github.com/ryuhaneul/dt-agent.git ductor
cd ductor
git rev-parse HEAD
./scripts/onboard.sh
```

`git rev-parse HEAD`는 설치한 정확한 버전을 기록하는 조회 명령이며 아무것도 바꾸지 않습니다. 문제 보고와 업데이트 후 되돌릴 버전의 기준으로 SHA를 보관하십시오.

대화형 초기 설정은 stdin과 stdout이 **모두** TTY일 때만 실행합니다. 호스트 home(기본 `~/.ductor`), Telegram 토큰(숨김 입력), 허용 사용자 ID, 시간대(기본 **감지한 호스트 시스템 시간대, 실패 시 UTC**), 봇 UI 언어(서버 로케일 기본, 지원 밖이면 `en`), 추가 전송 방식, API·webhook, bind 주소를 묻습니다. 그룹 ID는 대화형 질문이 아니므로 `--allowed-groups`로 전달합니다. 비밀 입력의 앞뒤 공백은 제거합니다. Telegram 토큰은 ASCII 숫자 접두부·콜론·비어 있지 않은 영숫자/밑줄/하이픈 뒷부분이어야 합니다. 내부 공백이나 다른 문자는 파일 작성 전에 거부하며 오류에 토큰을 표시하지 않습니다. 처음 설치할 때 토큰·사용자 ID를 빈 값으로 둘 수 없고 비TTY에서는 실제 필수 입력을 지정해야 합니다. 재실행 질문의 기본값은 저장된 값이며 Enter로 유지합니다. 숨김 토큰 입력도 빈 응답이면 기존 토큰을 유지합니다. 비대화형 재실행은 새 값을 지정하지 않으면 저장된 자격·사용자 ID를 사용합니다.

비대화형 설치에서는 토큰이 셸 기록에 남지 않도록 비공개 파일을 만들고 **파일 경로**를 전달합니다. `123456789`는 실제 숫자 사용자 ID로 바꾸십시오.

```bash
umask 077
read -r -s -p "Telegram bot token: " BOT_TOKEN; printf "\n"
printf "%s\n" "$BOT_TOKEN" > "$HOME/ductor-token"
unset BOT_TOKEN
chmod 600 "$HOME/ductor-token"
INSTALL_HOME="$HOME/.ductor"
./scripts/onboard.sh --no-prompt --home "$INSTALL_HOME" \
  --telegram-token-file "$HOME/ductor-token" --allowed-users 123456789 \
  --allowed-groups=-1001234567890 --tz Asia/Seoul --language ko --transport none \
  --no-api --no-webhook --bind 127.0.0.1
```

초기 설정은 선택한 호스트 home을 저장소 `.env`에 저장합니다. login·logout·update는 해당 `DUCTOR_HOME` 키를 자동으로 읽으므로 셸 export가 필요하지 않습니다. 비어 있지 않은 환경변수를 명시하면 저장값보다 우선하며, 둘 다 없으면 `~/.ductor`를 사용합니다. 봇 안 config의 home은 호스트 경로가 아닌 `/ductor`를 유지합니다.

위 초기 설정 방식 중 **하나**를 선택하십시오. 비대화형 예시는 예시 그룹을 허용하므로 새 home에서 DM 전용이면 그룹 옵션을 빼십시오. 재실행에서 그룹 옵션을 생략하면 기존 그룹을 유지하며, 명시적인 빈 --allowed-groups 값으로 목록을 비울 수 있습니다. 초기 설정은 루트 `.env`(Compose 설정), home의 `config/config.json`(메신저 자격 포함), `.env`(provider 키 주석 틀), `secrets.env`(도구 자격 증명용 빈 저장소, 없을 때만 생성), `.env.shared`(생성된 브라우저 토큰·TZ), `.cli-auth/`, `logs/`를 만듭니다. API·webhook을 켜면 루트 `compose.override.yml`을 생성하고 모두 끄면 해당 자동 생성 override를 제거합니다. 파일 권한은 0600, `.cli-auth/`는 0700입니다. 초기 설정은 빌드·기동·로그인이나 전체 workspace 생성을 수행하지 않습니다. 기존 파일은 `--force`가 없으면 유지하되 권한을 제한할 수 있고 기존 비밀 파일 심볼릭 링크는 거부합니다. 단 `secrets.env`는 예외로, 이미 있으면 심볼릭 링크이거나 권한이 느슨해도 `--force` 여부와 관계없이 그대로 두고 건드리지 않습니다. `--force`는 이번 옵션·환경변수·대화형 답으로 받은 설정만 갱신하고 그룹·heartbeat·리스너 포트·임의 키 등 나머지 기존 값을 보존합니다. 예제에서 새로 생긴 키는 중첩 설정에도 없는 것만 추가합니다. 그룹은 --allowed-groups/ONBOARD_ALLOWED_GROUPS를 지정할 때만 바뀌며 질문은 추가되지 않습니다. --force 전에 백업하십시오. 설정을 완전히 새로 만들려면 DUCTOR_HOME/config/config.json을 지우고 초기 설정을 다시 실행하십시오.

초기 설정은 아래 다음 단계 안내로 끝납니다. 로그인 명령은 저장된 config의 provider를 사용하며 기본값은 Claude입니다. 아래 예시는 언어로 `ko`를 선택한 경우입니다.

```text
봇 언어: ko
온보딩 설정이 준비됐어요. 다음 단계:
  docker compose up -d --build
  docker compose logs --tail=100 ductor-bot
  provider에 로그인하기 전에 Bot online이 나오는지 확인하세요.
  ./scripts/login.sh claude
  ./scripts/status.sh
설정을 초기화하려면 DUCTOR_HOME/config/config.json을 지우고 온보딩을 다시 실행하세요.
```

표시된 명령을 저장소 루트에서 순서대로 실행하십시오. 브라우저가 healthy 상태가 되어야 봇이 시작합니다. 로그에서 `Bot online`을 확인한 뒤 **실행 중인** 봇 컨테이너에서 선택한 CLI의 OAuth·device 인증을 완료하고 status를 확인하십시오. 필요하면 `docker compose logs -f ductor-bot`으로 로그를 따라볼 수 있습니다.

로그 따라보기를 끝낼 때 Ctrl-C를 눌러도 봇은 종료되지 않습니다. 허용한 Telegram 개인 채팅에서 **Start**를 누르고 `/status`, 이어서 “인사하고 지금 쓰는 모델을 알려줘” 같은 일반 메시지를 보내십시오. 그룹에서는 main 봇을 추가하고 실제 그룹·사용자 허용 목록을 설정한 뒤 원하는 토픽에서 메시지를 보내십시오. Telegram이 제공하는 DM 토픽도 토픽 식별값을 유지합니다. Codex·Gemini·Grok은 사용하는 경우에만 인증하고 `/model` 또는 설정으로 모델·provider를 바꾼 뒤 `/status`로 확인하십시오.

### 첫 대화: 페르소나와 행동 원칙

각 에이전트 workspace에는 한 번만 생성되고 이후 덮어쓰지 않는 두 파일이 들어 있습니다. `PERSONA.md`는 이름·역할·사용자 호칭·말투·응답 언어를 적는 빈 양식(미설정 표지 포함)이고, `BEHAVIOR.md`는 항상 지켜야 할 필수 원칙만 담은 짧은 문서입니다. 두 파일은 시스템 프롬프트에 추가되며(`append_system_prompt_files`), 아직 미설정인 페르소나는 주입하지 않습니다. `PERSONA.md`에 미설정 표지가 남아 있는 동안에는 프레임워크가 화면에 보이지 않는 짧은 설정 안내를 사용자 메시지 뒤에 붙입니다(세션 첫 턴, 이후 6번째 메시지마다). 에이전트는 자신과 사용자에 대해 묻고, `PERSONA.md`를 채워 표지를 지우며, 사용자에 대한 사실은 `memory_system/`에 저장하고 `BEHAVIOR.md`에는 반드시 지켜야 할 것만 최소로 추가합니다. 안내는 `"persona_setup_prompt": false`로 끌 수 있습니다(핫 리로드). Claude CLI에서는 `PERSONA.md`·`BEHAVIOR.md`·`MAINMEMORY.md`의 수정이 바로 다음 턴부터 반영됩니다(`claude_system_prompt_snapshot: "off"`, 기본값. `"on"`이면 이어가는 세션이 시작 때의 텍스트를 유지하는 이전 동작으로 돌아가며, 다른 provider는 각자의 반영 시점을 따릅니다. [상세](docs/modules/cli.md#claude-system-prompt-snapshot)). `MAINMEMORY.md`는 필수 내용과 인덱스만 두고 세부 내용은 `memory_system/` 아래 주제 파일에 나눕니다. 모델과 상관없이 중요한 내용은 여기에 저장하고, 모델 자체 메모리(예: Claude auto-memory)는 그 모델에만 해당하는 메모에만 씁니다. 모든 에이전트 workspace에는 DT Agent 자체의 참조 문서(사용법·모든 설정 키·명령·구조)가 프레임워크 관리 파일로 `docs/dt-agent/`에 동봉되며(`INDEX.md`에서 시작, `memory_search.py --docs`로 검색), `python3 scripts/build_agent_docs.py`로 생성되어 업데이트마다 교체됩니다. 에이전트는 `tools/memory_tools/memory_search.py`로 페이지를 찾고 편집한 뒤 `memory_lint.py check`로 점검합니다. 프레임워크도 주기마다(`memory_lint`: `mode`, `every_hours` 168, `report` `topic`/`dm`/`off`) 같은 점검을 직접 실행합니다. `mode`가 `structure`(기본)이면 문제가 없을 때 비용이 들지 않고, 문제가 있으면 Telegram 봇이 에이전트 턴을 한 번 실행해 안전한 것은 고치고 나머지를 알려 줍니다. `full`이면 위키가 깨끗해도 주기마다 에이전트 턴을 한 번 실행해 중복·모순을 찾고, 월 1회 낡은 규칙을 점검해 보고하며, 지시문은 사용자가 소유하고 업데이트가 덮어쓰지 않는 `tools/memory_tools/LINT_FULL.md`입니다. `off`는 아무것도 실행하지 않습니다. 턴은 봇의 개인 채팅이 토픽 모드면 전용 "Memory Lint" 토픽에, 아니면 DM에서 진행합니다.

## 3. 초기 설정 옵션 전체와 추가 메신저

| 옵션 | 동작 |
| --- | --- |
| `-h`, `--help` | argparse 도움말. 래퍼는 먼저 Python·Docker·Compose를 검사합니다. |
| `--no-prompt` | TTY 질문을 끕니다. Telegram 토큰·사용자 필수 검사는 유지되며 저장된 값을 다시 사용할 수 있습니다. |
| `--home PATH` | 호스트 영속 home. ~를 확장하고 절대 경로로 해석합니다. |
| `--telegram-token-file FILE` | 비어 있지 않은 한 줄의 앞뒤 공백을 제거하고 숨김 입력과 같은 Telegram 토큰 형식을 검사합니다. 옵션에 토큰 자체를 넣지 마십시오. |
| `--allowed-users IDS`, `--allowed-groups IDS` | 쉼표로 구분한 숫자 ID. 사용자는 필수, 그룹은 선택입니다. 음수 그룹 목록은 = 형식을 쓰십시오. |
| `--tz ZONE` | IANA 시간대. 생략하면 저장값 유지. 처음 설치에서는 시스템 시간대 감지, 실패하면 UTC. |
| `--language CODE` | 봇 UI 언어. 명시한 코드는 지원 값이어야 합니다. 재실행은 저장값이 기본이며 처음 설치는 서버 로케일, 대체값은 en입니다. |
| `--transport none|matrix|slack` | 반복 지정 가능한 추가 메신저. Telegram은 유지됩니다. none은 다른 값과 함께 쓸 수 없습니다. |
| `--matrix-homeserver URL`, `--matrix-user-id ID` | Matrix 추가 시 필수입니다. |
| `--matrix-password-file FILE`, `--matrix-access-token-file FILE` | Matrix 추가 시 둘 중 정확히 하나. 자격은 비공개 config에 저장됩니다. |
| `--matrix-device-id ID` | Matrix access token 사용 시 필수입니다. |
| `--matrix-allowed-rooms IDS`, `--matrix-allowed-users IDS` | 쉼표로 구분한 문자열. 적어도 한 허용 목록은 비어 있지 않아야 합니다. |
| `--slack-bot-token-file FILE`, `--slack-app-token-file FILE` | Slack 추가 시 둘 다 필수입니다. |
| `--slack-allowed-users IDS`, `--slack-allowed-channels IDS` | 쉼표로 구분한 문자열. 적어도 한 허용 목록이 필요합니다. |
| `--api` / `--no-api`, `--webhook` / `--no-webhook` | 리스너 활성·비활성. 기본 비활성입니다. API는 api 빌드 extra도 추가합니다. |
| `--bind IP` | API·webhook 공개 포트의 숫자 IP bind. 기본 127.0.0.1입니다. |
| `--force` | 파일 갱신을 허용합니다. config의 미지정 값은 보존하고 없는 기본 키만 추가하며 env 파일은 여전히 교체할 수 있습니다. |

우선순위는 명시적 인자 → 환경 변수 → 기본값·TTY 질문입니다. 환경 변수 대응은 `DUCTOR_HOME`; `ONBOARD_TELEGRAM_TOKEN_FILE`, `ONBOARD_ALLOWED_USERS`, `ONBOARD_ALLOWED_GROUPS`, `ONBOARD_TZ`, `ONBOARD_LANGUAGE`, `ONBOARD_TRANSPORTS`; `ONBOARD_MATRIX_HOMESERVER`, `ONBOARD_MATRIX_USER_ID`, `ONBOARD_MATRIX_PASSWORD_FILE`, `ONBOARD_MATRIX_ACCESS_TOKEN_FILE`, `ONBOARD_MATRIX_DEVICE_ID`, `ONBOARD_MATRIX_ALLOWED_ROOMS`, `ONBOARD_MATRIX_ALLOWED_USERS`; `ONBOARD_SLACK_BOT_TOKEN_FILE`, `ONBOARD_SLACK_APP_TOKEN_FILE`, `ONBOARD_SLACK_ALLOWED_USERS`, `ONBOARD_SLACK_ALLOWED_CHANNELS`; `ONBOARD_API`, `ONBOARD_WEBHOOK`, `ONBOARD_BIND`입니다. 불리언 환경 값은 true/false·yes/no·1/0·y/n을 허용합니다. 환경 변수의 메신저 목록은 쉼표로 구분합니다.

시간대 우선순위는 `--tz` → `ONBOARD_TZ` → 재실행의 저장된 시간대 → 호스트 시스템 시간대 → `UTC`이며, 시스템 감지는 `ZoneInfo`로 유효성을 확인한 `TZ`, `/etc/timezone`, `/etc/localtime` 심볼릭 링크의 `zoneinfo/` 뒤 경로 순서입니다. 대화형 질문은 저장값 또는 감지 값을 기본값으로 표시합니다.

언어 우선순위는 `--language` → `ONBOARD_LANGUAGE` → 재실행의 저장된 언어 → 서버 로케일 → `en`입니다. 비어 있지 않은 첫 `LC_ALL`·`LC_MESSAGES`·`LANG`에서 언어 부분을 추출하고(예: `ko_KR.UTF-8` → `ko`) 지원하지 않으면 `en`으로 대체합니다. 포함된 언어는 `de`, `en`, `es`, `fr`, `id`, `ko`, `nl`, `pt`, `ru`이며 옵션·환경변수·질문 응답으로 명시한 지원 밖 값은 파일을 쓰기 전에 오류로 종료합니다. 5번 질문에는 저장값 또는 감지한 기본값이 표시되며 Enter로 유지합니다. 결정된 언어는 봇 UI 메시지뿐 아니라 온보딩의 질문·오류·완료 안내와 `onboard.sh`의 사전 검사 메시지(python3·docker·compose 없음, root 실행·Python 3.9 미만)에도 적용됩니다. 5번 질문에서 언어를 바꾸면 이후 문구가 새 언어로 나옵니다. 명령·옵션·언어 코드·경로는 번역하지 않으며 명령줄 오류와 `--help` 출력은 선택한 언어로 표시됩니다. `--force`가 없으면 기존 config를 보존하므로 완료 안내는 저장된 언어를 표시합니다. 옵션 이름은 줄여 쓸 수 없고, `--lang=ko` 같은 줄임 표기는 결정된 언어의 알 수 없는 인자 오류(종료 코드 2, 아무것도 쓰지 않음)가 됩니다.

아래 예시는 **새** home에 Telegram과 Matrix를 함께 추가합니다(예시 ID·경로를 모두 바꾸십시오). 이후 Matrix만 사용하려면 config의 `transports`를 `["matrix"]`, `transport`를 `"matrix"`로 바꾸십시오. access token 방식에는 해당 `--matrix-device-id`도 필요합니다. room·user 인증 기준은 Telegram과 다르므로 [Matrix 설정](docs/matrix-setup.md)과 현재 [설정 문서](docs/config.md)를 참고하십시오.

```bash
./scripts/onboard.sh --no-prompt --home "$HOME/.ductor-matrix" \
  --telegram-token-file "$HOME/ductor-token" --allowed-users 123456789 \
  --transport matrix --matrix-homeserver https://matrix.example.org \
  --matrix-user-id @ductor:matrix.example.org \
  --matrix-password-file "$HOME/matrix-password" \
  --matrix-allowed-rooms '!room:matrix.example.org'
docker compose up -d --build
```

기존 home에서는 초기 설정 인자만 바꿔도 저장된 config·루트 env가 덮어써지지 않습니다. 저장된 config와 루트 `DUCTOR_EXTRAS`를 함께 수정하고, `--force` 없이 초기 설정을 다시 실행하면 저장된 config 기준으로 포트를 재생성합니다. 지정한 설정 변경을 저장하려면 env 파일을 백업하고 --force로 실행하십시오. 미지정 설정과 기존 리스너 포트는 유지됩니다. extras 변경에는 재빌드가 필요합니다. API 기본 포트는 8741, webhook은 8742이며 켠 main 리스너만 공개합니다. 인증 토큰은 처음에 빈 값이며 켠 리스너를 시작할 때 생성·저장합니다. 저장된 비공개 config를 로컬에서 확인하거나 `api.token`·`webhooks.token`을 직접 설정하고 설정 문서의 인증·공개 제한을 따르십시오. 브라우저 토큰은 별도입니다.

## 4. 조작 스크립트와 provider 슬롯

아래는 호스트 스크립트입니다. 저장소 루트에서 실행하십시오. `login.sh`는 Compose 서비스명을, `logout.sh`·`status.sh`는 `DUCTOR_CONTAINER`(기본 `ductor-bot`)를 사용합니다. `update.sh`의 이름은 `ductor-bot`으로 고정되어 있습니다. login·logout·update의 호스트 home 우선순위는 비어 있지 않은 환경변수 `DUCTOR_HOME` → 스크립트 위치 기준 저장소 루트 `.env`의 해당 키(따옴표 처리) → `~/.ductor`입니다. 해당 키만 읽고 env 파일을 source하지 않습니다. `_home.sh`는 내부 공통 헬퍼이며 조작 명령이 아닙니다. 인자 파서가 없는 status·rebuild·update에는 동작하는 `--help`가 없으며 추가 인자를 주어도 평소 동작을 실행합니다.

| 스크립트 / 인터페이스 | 동작과 예시 |
| --- | --- |
| `onboard.sh` / `onboard.py` | 선행 조건 래퍼 / 비공개 config 작성기. 옵션은 3절. 예: `./scripts/onboard.sh --help`. |
| `login.sh [provider]` | 인자 없음: Claude → Codex → Gemini → Grok. 자격 확인 실패는 경고 후 계속 진행하고 마지막에 요약합니다. provider는 claude/codex/gemini/grok, -h/--help/help 지원. 예: `./scripts/login.sh codex`. |
| `login.sh claude [slot]` | 빈 값·default·claude는 claude, alt는 claude-alt, 완전한 claude-*도 허용합니다. --list/-l/list는 슬롯 조회(공유 디렉터리 생성), --repair/repair는 비자격 항목을 백업 후 공유 링크로 옮기며 claude -h/--help도 지원합니다. 예: `./scripts/login.sh claude alt`. |
| `logout.sh [provider]` | 인자 없음은 전체 로그아웃. claude/codex/gemini/grok, -h/--help 지원. Claude --list(인증·우선순위·남은 쿨다운, 컨테이너 없이 조회), --prefer SLOT, --priority A,B,..., --clear-cooldown SLOT 또는 all(변경은 실행 중인 컨테이너 필요), --disable SLOT(config만), --remove SLOT 또는 SLOT(config와 자격 백업 이름 변경). 예: `./scripts/logout.sh claude --disable alt`. Codex는 저장 인증 파일을 제거하고 Gemini는 `oauth_creds.json`을 제거하고 키 삭제·재시작 안내를 출력합니다. |
| `status.sh` | 파싱하는 옵션 없음. 컨테이너·CLI 인증 탐침. 예: `./scripts/status.sh`. Gemini는 API 키 환경 또는 OAuth 자격증명 파일을 확인합니다. |
| `rebuild.sh` | 파싱하는 옵션 없음. CACHEBUST와 --pull로 빌드 후 up -d·ps. 소스 pull 없음. 예: `./scripts/rebuild.sh`. 컨테이너 교체와 가변 CLI 버전 갱신을 수행합니다. |
| `update.sh` | 파싱하는 옵션 없음. 추적 파일 변경 검사·origin fetch·추적 브랜치 ff-only pull·확인 질문·재빌드·교체. 예: `./scripts/update.sh`. 교체를 거부하면 새 소스와 기존 실행 이미지가 공존할 수 있으며 자동 롤백은 없습니다. 턴이나 작업이 실행 중이면 교체 직전에 다시 묻고, 거부하면 새 이미지는 빌드된 채 기존 컨테이너가 계속 실행됩니다. |

필요한 provider만 개별 인증하십시오. Gemini는 대화형 CLI를 시작하며 컨테이너에 `GEMINI_API_KEY` 또는 `GOOGLE_API_KEY`가 있으면 OAuth 파일 확인을 건너뜁니다. 원격 서버에서 Google OAuth를 쓰면 해당 CLI 안내에 따른 SSH loopback 포워딩이 필요할 수 있습니다. Grok은 device 인증, Codex 스크립트는 `codex login --device-auth`를 사용합니다. Claude 대체 슬롯은 기본 슬롯과 프로젝트 세션·비자격 설정을 공유하지만 자격 파일은 별도입니다. 로그인 성공 후 인증된 Claude 슬롯이 둘 이상이면 스크립트가 `["claude", "claude-alt"]`처럼 순서 있는 `claude_accounts` 목록을 제안합니다. 등록 질문은 기본 Y입니다. 빈 입력·y/yes·비TTY 실행은 새로 인증된 슬롯을 저장 시점의 최신 config.json 목록에 병합하고, n/no는 등록하지 않습니다. 다른 설정과 기존 파일 권한은 보존합니다. 갱신 실패 시 0이 아닌 종료 코드로 끝나며 Updated 안내를 출력하지 않습니다. 우선순위는 main에 몇 초 안에, 하위 에이전트는 다음 기동 때 반영됩니다. 쿨다운 해제는 모두 다음 호출부터 반영됩니다. `--disable`·`--remove`는 원자적으로 목록을 수정하고 이후 호출은 목록을 다시 읽으며 이미 진행 중인 호출은 기존 슬롯 목록을 유지합니다.

표준 이미지는 `/etc/claude-code/managed-settings.d/10-dt-agent-retention.json`에 `cleanupPeriodDays: 36500`을 설정해 슬롯과 무관하게 컨테이너 안 모든 Claude 실행(봇 호출·스트리밍·원샷·하위 에이전트·로그인·직접 실행)에 적용합니다. 조직의 서버 정책처럼 우선순위가 높은 managed 출처는 이 로컬 정책을 대체할 수 있고, 관리자는 같은 디렉터리에 이름이 더 뒤인 drop-in을 두어 값을 의도적으로 바꿀 수 있습니다([Claude Code의 managed 출처 결합 규칙](https://code.claude.com/docs/en/managed-settings#how-claude-code-combines-managed-sources)).
대화 기록은 `DUCTOR_HOME` 아래 계속 쌓여 디스크를 사용하며 `CleanupObserver`는 이를 지우지 않습니다.
managed 설정이므로 사용자 설정으로 보존 기간을 줄일 수 없습니다. 더 짧게 보존하려면 이름이 더 뒤인 drop-in을 추가하고 이미지를 다시 빌드하십시오.

```bash
./scripts/login.sh claude --list
./scripts/login.sh claude alt
./scripts/login.sh codex
./scripts/login.sh gemini
./scripts/login.sh grok
./scripts/logout.sh --help
```

컨테이너 안 제품 CLI는 인자 없음(설정된 봇 기동), --help/help·--version·--verbose·status, agents [list|add NAME|remove NAME]을 지원합니다. add는 Telegram 중심 대화형 생성이며 headless·Matrix는 agents.json에 직접 정의할 수 있습니다. 확인 목적으로 활성 home에 인자 없는 ductor를 또 실행하지 마십시오.

```bash
docker compose exec ductor-bot ductor --help
docker compose exec ductor-bot ductor --version
docker compose exec ductor-bot ductor status
docker compose exec ductor-bot ductor agents list
```

## 5. Telegram·Matrix 채팅 명령

셸 명령이 아닌 **채팅 메시지**입니다. Matrix는 !명령과 /명령을 모두 허용합니다. Telegram 버튼과 Matrix 반응·텍스트 메뉴 등 UI는 다릅니다. `/interrupt`는 Telegram 일반 핸들러가 아닌 미들웨어가 처리합니다. main 전용 에이전트 관리 명령에는 실행 중인 supervisor가 필요합니다.

| 채팅 명령 | 용도 |
| --- | --- |
| `/start` | 시작·도움말. Telegram Start 버튼. |
| `/help` | 명령 도움말. |
| `/info` | 시스템·workspace 정보. |
| `/new` | 이 채팅·토픽의 설정된 기본 provider 세션 초기화. |
| `/reset` | 현재 활성 provider 세션 초기화. |
| `/stop` | 이 토픽의 실행 중 작업을 중단하고 뒤에서 기다리던 메시지를 취소합니다(응답에 취소한 개수 표시). |
| `/interrupt` | 활성 CLI 작업에 SIGINT로 부드러운 중단. |
| `/stop_all` | supervisor가 있으면 에이전트 전체 작업 중단. |
| `/model [name]` | 모델·provider 선택. 인자 없으면 선택 UI. |
| `/accounts` | main Telegram 봇 전용: Claude 슬롯의 인증·우선순위·쿨다운·저장된 사용량 조회, 순서 변경·쿨다운 해제. |
| `/usage` | Telegram main·하위 봇 모두: 읽기 전용 사용량. Claude는 평소 실행에서 받은 마지막 값, Codex는 명령·새로고침 때 조회. |
| `/effort` | provider 추론 수준 선택. |
| `/status` | 현재 세션·모델·실행 상태. |
| `/secret` | main Telegram 봇 전용, 허용 사용자의 1:1 채팅: 도구 시크릿 입력·키 목록·삭제·취소. 값은 입력 요청에 답장으로 보냅니다. |
| `/memory` | 주요 메모리 표시. |
| `/session [prompt]` | 이름 있는 백그라운드 세션 제출. @provider·model·session 선택 지시 가능. 인자 없으면 도움말. |
| `/sessions` | 이름 있는 세션 조회·관리. |
| `/tasks` | 백그라운드 작업 조회·관리. |
| `/cron` | 예약 작업 개요. |
| `/agent_commands` | 설정된 에이전트 명령 메뉴. |
| `/showfiles` | workspace 파일 탐색. |
| `/diagnose` | 실행 진단. |
| `/restart` | 이 에이전트 재시작. 작업 중단 발생. |
| `/agents` | main 전용: 에이전트 개요. |
| `/agent_start NAME` | main 전용: 설정된 에이전트 기동. |
| `/agent_stop NAME` | main 전용: 에이전트 종료. |
| `/agent_restart NAME` | main 전용: 에이전트 재시작. |

Telegram 버튼에 `/restart`·`/status`처럼 ductor가 아는 슬래시 명령이 있으면 직접 입력했을 때와 같은 처리기로 실행하며 인증·봇 지정/mention·명령 필터도 적용합니다. 선택한 버튼 표시는 유지하고 모르는 슬래시 명령은 AI 프롬프트로 처리합니다. 텍스트·미디어·프롬프트 버튼은 CLI 실행 전에 세션에서 선택한 provider 인증을 매번 확인하며 미인증이면 현지화된 `./scripts/login.sh <provider>` 안내를 반환합니다. 로그인은 재시작 없이 다음 사용자 턴에 반영됩니다. Matrix·Slack·직접 API 사용자 턴에도 적용하며 에이전트 간 위임·cron 실행에는 적용하지 않습니다.

이름 있는 세션은 `@<정확한-세션-이름> 메시지`로 이어갈 수도 있습니다. 에이전트 간 세션 이름은 발신자·채팅·토픽 범위에 따라 정해지므로 추측하지 말고 보고된 이름을 복사하거나 `/sessions`에서 확인하십시오. Matrix는 내부에서 room을 숫자 세션 ID로 매핑합니다. 세션은 transport·chat·topic과 provider별로 저장 영역을 분리합니다(같은 chat ID의 API·Telegram 세션 공유는 [알려진 한계](#12-문서알려진-한계라이선스) 참고). Telegram 그룹·개인 채팅 메시지는 `is_topic_message`가 참일 때만 `message_thread_id`를 토픽 범위로 사용합니다. 그 외에는 thread ID가 있어도 `topic_id`는 `None`입니다.

## 6. 저장소·컨테이너·영속 home 구조

```text
ductor_bot/           # tracked Python package
  __main__.py         # bot CLI entry point
  cli_commands/       # help/status/agents CLI
  cli/                # provider subprocesses, auth, process registry
  messenger/          # Telegram / Matrix / Slack transports
  orchestrator/       # turns, commands, lifecycle
  bus/                # envelopes and async delivery
  multiagent/         # supervisor, registry, inter-agent bus/API
  session/            # transport/chat/topic and named sessions
  tasks/, background/ # background tasks and named-session work
  cron/, webhook/, heartbeat/, cleanup/
  workspace/          # path model, seeding, rule/skill sync
  infra/, security/, files/, text/, api/, i18n/
  _home_defaults/     # bundled home/workspace templates
docker/               # ductor-bot, browser, env templates
scripts/              # host operators and isolated verifiers
tests/                # product tests
docs/                 # configuration and module docs
compose.yaml          # two-service runtime
config.example.json   # onboarding input template
pyproject.toml        # package/dependencies/extras
justfile              # developer checks
```

봇 이미지에는 openssh-client(`ssh`·`scp`)가 기본 포함됩니다. Python paramiko는 설치하지 않으며 PyNaCl은 `api` extra로만 제공합니다.

Compose는 `ductor-bot`, `ductor-browser`, 명시적으로 이름 붙인 `ductor-net`을 만듭니다. 봇은 `init: true`, `restart: unless-stopped`, `stop_grace_period: 30s`를 사용합니다. 브라우저도 같은 재시작 정책과 내부 18800 포트의 HTTP healthcheck를 사용합니다. 브라우저에는 호스트 포트·호스트 home 마운트·Docker socket이 없습니다. 봇은 호스트 `DUCTOR_HOME`을 `/ductor`에 bind-mount하고 home `.env`와 `.env.shared`를 읽으며 브라우저는 `.env.shared`만 읽습니다. 기본 Compose에는 **공개 호스트 포트가 없습니다**. 초기 설정 override가 활성 main API·webhook 포트를 기본 loopback 주소로 공개합니다. 내부 에이전트 API는 localhost 8799를 사용합니다.

중첩 CLI sandbox·Docker socket 마운트나 호스트 서비스 설치는 없습니다. 봇 컨테이너는 `/ductor`에 마운트된 내용을 수정할 수 있고 설정의 기본 permission mode는 provider 승인 질문을 생략합니다. home 하나에는 봇 컨테이너 하나만 실행하십시오. 컨테이너·네트워크 이름이 고정되어 있으므로 프로젝트명만 바꿔서는 다른 복사본을 격리할 수 없습니다. 복사본을 따로 띄우려면 컨테이너·네트워크·이미지 이름을 바꾸는 override가 필요합니다.

```text
$DUCTOR_HOME/                     # host; mounted as /ductor
  config/config.json              # main settings and messaging credentials
  .env                            # provider keys read when a CLI starts
  secrets.env                     # tool credentials (0600); loaded on demand, not injected
  .env.shared                     # BROWSER_TOKEN and TZ; both services
  .cli-auth/                      # persistent CLI auth (0700 at onboarding)
    claude/, claude-*/            # independent credentials, shared settings
    _shared-projects/, claude.json
    codex/, gemini/, grok/
  agents.json                     # JSON array of sub-agent definitions
  agents/<name>/                  # independent sub-agent home
    config/config.json, workspace/, logs/
  workspace/                      # seeded at runtime
    tools/, skills/, memory_system/, cron_tasks/, tasks/
    output_to_user/, telegram_files/, matrix_files/, api_files/
  logs/agent.log
  sessions.json, named_sessions.json, tasks.json
  cron_jobs.json, webhooks.json, startup_state.json
  inflight_turns.json, chat_activity.json, heartbeat_state.json
  interagent/inflight/, interagent/undelivered/
  SHAREDMEMORY.md, bot.pid, restart-requested
```

모든 파일이 즉시 생기지는 않으며 시작·기능 사용 시 생성됩니다. main `DuctorPaths`는 `/ductor`, 서브 home은 `/ductor/agents/<name>`을 사용합니다. CLI 계정 자격은 컨테이너 공통 인증 환경에 있으므로 workspace·상태 분리가 별도 유료 provider 계정을 뜻하지는 않습니다. 사용자 소유 틀은 한 번 생성하지만 프레임워크 관리 규칙·도구 스크립트는 덮어쓰기·동기화되므로 사용자 변경은 관리 도구 밖에 두십시오. 변경 전에 home과 배포 설정을 백업하십시오.

## 7. 멀티 에이전트 설정과 위임

main과 서브는 같은 supervisor·컨테이너에서 실행됩니다. `$DUCTOR_HOME/agents.json`에는 JSON **배열**을 넣으십시오. 아래 예시는 봇 없는 helper와 자기 Telegram 봇을 가진 messenger입니다. messenger 토큰은 비공개로 교체해야 하며 예시 값은 동작하는 토큰이 아닙니다. 파일 권한은 0600으로 설정하십시오. 서브는 main 기본 설정에 null이 아닌 registry override를 적용하지만 자격·허용 목록은 자기 것을 사용하며 main의 Telegram 접근 권한을 물려받지 않습니다. 생성된 `agents/<name>/config/config.json`은 실행 결과물이므로 원본인 `agents.json`을 수정하십시오.

```json
[
  {"name": "helper", "transport": "headless", "provider": "claude", "model": "sonnet"},
  {"name": "messenger", "transport": "telegram",
   "telegram_token": "REPLACE_PRIVATELY", "allowed_user_ids": [123456789],
   "allowed_group_ids": [], "interagent_notify_received": false}
]
```

**headless**는 main의 동기·비동기 위임으로만 쓰는 작업자입니다. 자기 workspace는 있지만 봇 토큰·채팅이 없어 사용자가 직접 대화할 수 없습니다. 별도 직접 채팅이 필요하면 Telegram·Matrix 서브를 선택하십시오. 에이전트는 프레임워크 CLI 턴에서 다음처럼 생성할 수 있습니다.

```text
python3 tools/agent_tools/create_agent.py --name helper --transport headless \
  --provider claude --model sonnet
```

`--provider`·`--model`은 선택 사항이며 호스트 설치 명령이 아닌 내부 에이전트 도구입니다. 비동기 결과는 main을 거쳐 요청한 원 채팅·토픽으로 돌아옵니다.

서브를 만들기 전에 main은 사용자에게 역할·호칭·말투와 언어·꼭 필요한 행동 원칙을 물어 `--persona`/`--persona-file`(필요할 때만 `--behavior`/`--behavior-file`)로 전달합니다. `--persona`가 없으면 봇 기반 서브는 빈 양식으로 시작해 첫 채팅에서 스스로 묻고, 채팅이 없는 headless 작업자는 main이 생성 시점에 받아 전달해야 합니다. 기존 파일은 덮어쓰지 않습니다. 서브가 받는 모든 위임 요청에는 위임 작업 규칙(이 턴 안에서 끝내기, 백그라운드 태스크로 넘기지 않기, 1시간 안에 못 끝나면 분할 요청) 짧은 블록이 붙습니다. 레인을 지정한 요청에는 한 줄이 더해집니다(다른 레인이 동시에 실행됨, 저장소 변경은 레인 전용 git worktree에서, 공유 메모리 파일은 지시가 있을 때만). 질문과 답에는 블록이 붙지 않습니다.

messenger 봇의 개인 채팅에서 한 번 Start를 누르십시오. headless 결과 알림은 처리한 세션 이름만 표시하고 존재하지 않는 서브 채팅에서 이어가라고 안내하지 않습니다. 위임 전용 서브는 main 봇의 그룹에 가입할 필요가 없으므로 그룹 허용 목록을 비워 둘 수 있습니다. 수신 알림을 켜서 그룹에 알림을 보내려면 서브의 **자기** 봇이 그 그룹에 접근할 수 있어야 하며 main이 대신 발송하지 않습니다. 서브의 전송 방식은 자기 Telegram·Matrix 자격으로 선택하고 명시한 전송 방식의 자격이 잘못되면 그 서브를 제외합니다. 전송 방식을 생략하고 자격이 없으면 headless입니다. headless는 위임 작업을 받고 동기 요청에 답할 수 있지만 비동기 호출을 발신하거나 비동기 결과 수신자가 될 수 없습니다.

에이전트 도구는 에이전트 신원과 원 채팅·토픽·전송 값이 자동 제공되는 **프레임워크 생성 CLI 턴 안에서** 실행합니다. 아래 경로는 해당 에이전트의 `workspace/` 기준이며 호스트 관리 명령이 아닙니다. 동기는 호출 에이전트에 도구 결과를 반환합니다. 비동기는 task ID를 반환한 뒤 결과를 호출 main의 원 채팅·토픽으로 전달하고 main 세션에 주입하여 main이 처리·보고하게 합니다. 일반 위임의 최종 결과를 서브 DM으로 우회하지 않습니다.

```text
python3 tools/agent_tools/ask_agent.py [--new] [--lane LANE] TARGET "message"
python3 tools/agent_tools/ask_agent_async.py [--new] [--lane LANE] [--summary "description"] \
  [--reply-to AGENT] [--silent] TARGET "message"
```

대괄호는 선택 인자를 뜻하므로 문자 그대로 입력하지 마십시오. `--new`는 수신자의 이름 있는 세션을 초기화합니다. `--lane LANE`(`a-z`·`0-9`·`-` 1~12자)은 같은 수신자에게 보내는 요청을 별도 병렬 세션으로 실행합니다. 같은 레인의 요청은 순서대로, 다른 레인은 `interagent.max_parallel_sessions`(기본 4)까지 병렬로 실행하며, 수신자가 바쁘면 거부하지 않고 대기열에 넣습니다. 레인은 수신자의 workspace와 로그인을 공유하므로 레인마다 파일과 git worktree를 따로 정하고 최종 병합 담당을 한 곳으로 두십시오([레인과 한도](docs/modules/multiagent.md#lanes-admission-and-the-time-contract)). 비동기 `--reply-to`는 다른 결과 수신 에이전트를 명시하므로 일반 main 위임에는 사용하지 않는 것이 맞습니다. **IA 턴의 비동기 역호출·중첩 호출은 거부**하므로 결과를 main으로 반환하십시오.

main이 동기로 위임하면 서브는 질문을 main에 보내지 말고 최종 보고에 넣어야 하며, 서버가 main 질문을 거부합니다. 모르는 값은 지어내지 말고 비워두거나 가정을 명시하십시오. 비동기 위임 중 main에 동기로 묻는 질문은 가능하면 원 채팅·토픽의 현재 세션에서 처리하며, 답은 서브에게만 가고 채팅에는 게시되지 않습니다. 이름 있는 세션이 출처이거나 출처를 확인할 수 없거나 세션 잠금을 60초 기다려도 얻지 못하면 출처 정보를 붙인 별도 세션에서 처리합니다. [라우팅 상세](docs/modules/multiagent.md#questions-during-delegation)를 참고하십시오.

`interagent_notify_received`는 **기본 false**이며 수신 서버가 결정합니다. 특정 서브만 켜려면 registry 값을 true로 설정하고 main의 true를 상속하려면 서브 override를 생략하십시오. 이 설정 적용에는 재시작이 필요합니다. false는 모든 수신 알림을 억제하며 `--silent`도 오류 없이 유효합니다. true면 수신 서브가 자기 봇으로 원 채팅에 수신 알림을 보내며 `--silent`는 호출별로 억제합니다. 결과 전달에는 영향이 없습니다.

재시작 시 봇이 꺼져 있는 동안 도착한 Telegram 메시지와 실행 중인 턴 뒤에서 기다리던 메시지는 재시작 후 처리하며(24시간이 지난 메시지는 짧은 안내와 함께 건너뛰고, 중단 명령으로 취소한 메시지는 되살리지 않습니다), 영속 inflight 작업은 중단 결과로 보고하거나 저장된 완료 결과를 복구하고, 아직 준비되지 않은 수신자의 결과는 보관·재시도·준비 이벤트로 재전달합니다. 정확히 한 번 전달을 보장하는 것은 아니므로 [알려진 한계](#12-문서알려진-한계라이선스)를 확인하십시오.

자동 이름으로 생성됐음이 확인된 DM 토픽은 수신한 사용자 메시지로 짧은 명사구 제목(예: “시스템 점검”)을 대화와 병렬로 생성합니다. main과 봇 있는 서브에 적용하며 `telegram_dm_topic_auto_name`은 기본 true·핫리로드 지원이고 false로 끌 수 있습니다. 모델이 쓸 만한 제목을 주지 못하면(null, 답변형, 결과형 문구) 사용자 메시지의 안전한 앞부분으로 토픽 이름을 정하고 거기서 끝납니다. 단순 인사나 이름 변경 실패는 다음 사용자 메시지에서 다시 시도하며 합쳐 최대 3회입니다. 오류·시간 초과·미인증도 같은 안전한 메시지 앞부분→토픽 ID 대체를 쓰고, 사용자 지정·편집 토픽과 그룹은 제외합니다. [DM 토픽 이름 상세](docs/modules/bot.md#automatic-dm-topic-names)를 참고하십시오.

AI로 전달되는 사용자 메시지(버튼 선택 포함), 에이전트 요청·결과·질문, 태스크 지시(생성·재개 및 부모 답 포함)·결과·질문과 에이전트 도구 반환에는 `user_timezone` 기준 `[Received: …]`가 붙습니다. 실제 세션 잠금 대기에는 `[Waited: …]`를 더하고, 대기 사용자 메시지는 기존 큐 블록 안에 시각을 넣어 중복하지 않습니다. Telegram 메시지·미디어가 보낸 뒤 10초 이상 늦게 봇에 도착하면 `[Received: …]` 바로 뒤에 같은 시간대의 `[Sent: …]`도 붙습니다(버튼 선택·Matrix·Slack은 제외). 비동기 결과 재시도·복구에도 최초 수신 시각을 보존합니다. AI 입력 metadata이며 채팅 원문·세션 미리보기·명령 로그·system prompt와 분리하지만 provider 대화 기록에는 실제 입력이 남습니다. heartbeat·cron·webhook 프롬프트에는 붙지 않습니다.

사용자 메시지가 실행 중인 세션 뒤에서 기다렸으면 Telegram·Matrix·Slack은 에이전트에게 `user_timezone` 기준 보낸 시각·잠금 대기 시간·대기 시작 순번을 전달합니다. 직전 답변에 대한 반응이 아니라 이전 작업과 관련된 메시지일 수 있다는 안내를 CLI 입력에만 붙입니다. 명령·버튼·취소된 항목·대기하지 않은 메시지에는 붙지 않으며, 대기 표시·세션 미리보기·명령 로그의 사용자 원문은 바뀌지 않습니다. [대기 메시지 문맥](docs/modules/messenger.md#queued-user-message-context)을 참고하십시오.

## 8. 설정·비밀값·안전한 진단

main 설정은 `$DUCTOR_HOME/config/config.json`입니다. [설정 스키마](docs/config.md), [예시](config.example.json), `ductor_bot/config.py`를 참고하십시오. 초기 설정은 **예시**를 복사하므로 스키마 기본값과 새 home의 값은 다를 수 있습니다. 모델은 스키마 opus·초기 설정 sonnet, legacy cli_timeout은 스키마 1800초·예시 600초, 초기 설정 시간대는 시스템 감지 값이며 실패하면 UTC입니다.

| 키 | 의미 |
| --- | --- |
| `provider/model/reasoning_effort` | 기본 CLI·모델·추론 수준. 세션 변경은 /model·/effort. |
| `allowed_user_ids/allowed_group_ids/group_mention_only` | 기본 그룹 허용 목록은 비어 있습니다. 그룹·사용자 모두 허용해야 하며 mention 조건은 인증 우회가 아닙니다. |
| `transports/matrix/slack` | 선택한 메신저·허용 목록을 활성화하고 대응 extras를 빌드합니다. |
| `telegram_dm_topic_auto_name` | 기본 true·핫리로드. 수신 시 주제를 생성하고 자동 이름 DM 토픽을 최종 한 번만 변경. |
| `interagent_notify_received` | 기본 false, 수신 측 알림 스위치. 재시작 필요. |
| `claude_accounts` | 인증된 계정 슬롯 순서. 자격은 .cli-auth에 유지됩니다. |
| `timeouts/cli_timeout/tasks` | 경로별 CLI 제한·작업 동작. 모든 경로가 동일한 global timeout을 쓰지는 않습니다. |
| `streaming/heartbeat/cleanup` | 스트리밍 출력·정기 확인·보관 기간. 예시는 heartbeat 활성, 수동 그룹 대상은 빈 목록. 끄려면 `config.json`의 `heartbeat.enabled`를 `false`로 설정합니다. 자동 대상 선정은 기본 켜짐으로, 최근 7일 안에 대화한 허용 Telegram DM·그룹 토픽 세션을 각각 선택합니다(사용자의 마지막 활동 기준, 추적·복구 대상은 이후에도 포함될 수 있음). suspend_on_ack도 기본 켜짐으로, 할 말 없이 ACK만 답한 세션은 다음 대화까지 쉽니다. 수락된 사용자·태스크·버스 활동이나 사용자 활동 횟수 변경도 다시 깨웁니다. 메모리 린트 보고와 크론 결과만으로는 heartbeat 대상이 되거나 깨어나지 않습니다. 조용한 시간 기본값은 꺼짐(0/0)이며, 최근 활동 쿨다운과 세션 조건은 계속 적용됩니다. |
| `cron_delivery_retry/cron_preflight` | 선택 cron 재시도(기본 꺼짐·300초·12회) / 결정적 건너뛰기 조건(기본 꺼짐·15초·HEARTBEAT_OK). |
| cron job `delivery_mode` | cron 도구로 job 마다 지정(`config.json` 아님): `separate`(별도 실행, 결과는 메시지로 도착; 전달 방식이 지정되지 않은 job), `inject`(chat/topic 에서 만든 job 의 기본값: 결과를 그 토픽 세션에 주입하고 세션 응답을 전달) 또는 `in_session`(별도 실행 없이 그 세션의 턴으로 지시를 수행). 둘은 chat 이 필요하며, 에이전트가 크론을 만들거나 고치기 전에 항목별로 설명합니다. [cron 전달 방식](docs/modules/cron.md#delivery-modes-inject-and-in_session) 참고. |
| `api/webhooks` | 선택 리스너. 인증 토큰·공개 제한은 브라우저 인증과 별도. |
| `file_access/permission_mode` | 스키마 기본 all/bypassPermissions. provider는 마운트 home에 접근합니다. |
| `language/user_timezone` | 스키마 언어 기본값은 en, 온보딩은 지원하는 서버 로케일 또는 --language/ONBOARD_LANGUAGE를 사용합니다. 한국어 UI는 ko. 자동화는 IANA 시간대. |
| `notifications.startup_targets` | 선택적 시작 알림 목적지. |

컨테이너는 Compose로 조작하십시오. 리스너 포트 변경에는 생성 override 갱신, 전송 방식·extras 변경에는 대응 빌드 의존성이 필요합니다. 일부 설정은 즉시 다시 읽지만 구조와 수신 알림 설정은 재시작하여 적용하십시오.

루트 `.env`는 provider 비밀값이 아닌 **Compose 설정**입니다. home `.env`는 CLI가 시작할 때 읽는 provider 키이고, home `secrets.env`는 도구가 필요할 때 읽는 자격 증명(CLI 실행에 주입하지 않음)이며, `.env.shared`는 두 서비스가 읽는 공용 브라우저 bearer 토큰·TZ입니다. 메신저·API·webhook 토큰은 비공개 config(서브 토큰은 비공개 agents.json), OAuth 자격은 `.cli-auth`에 저장됩니다. 비밀값·설정·인증은 Git 밖에 두고 0600·0700 권한을 사용하십시오.

CLI의 provider 키는 프로젝트 작업 디렉터리가 아닌 설정된 home에서 읽습니다. 도구 자격 증명은 `tools/_secrets.py`가 `secrets.env`에서 읽으며(경로는 `DUCTOR_SECRETS_FILE`로 전달), 환경에 이미 있는 값이 우선합니다. 기존 프로세스 환경이 우선이므로 Compose로 이미 컨테이너에 주입한 키는 home `.env` 수정으로 덮어쓸 수 없습니다. 필요하면 `docker compose up -d`로 env 변경을 반영하여 재생성하십시오. `docker compose restart`만으로 컨테이너 환경을 다시 읽지는 않습니다. 내부 dotenv 파서는 따옴표·export를 지원하지만 `$VARIABLE` 치환은 하지 않으므로 최종 값을 직접 쓰십시오. 진단을 위해 비밀 파일 전체를 source하거나 출력하지 마십시오.

값은 환경변수로 주입되지 않지만 같은 사용자로 실행되는 에이전트는 이 파일을 읽을 수 있습니다. 보호 범위는 우발적 노출 방지까지입니다.

잘못된 정의·병합 오류는 안전한 항목·필드·예외 메타데이터를 기록하고 환경 대입 마스킹은 지정된 민감 키를 처리합니다. 임의 사용자 프롬프트·provider 출력의 비밀값까지 모두 막는다는 보장은 아닙니다. 로그 공유 전 확인하십시오. 브라우저 `/health`는 무인증이고 다른 브라우저 동작에는 공용 bearer 토큰이 필요합니다.

## 9. 운영·업데이트·재빌드·롤백

```bash
docker compose ps
docker compose logs --tail=100 ductor-bot
docker compose logs --tail=100 browser
docker compose stop
docker compose up -d
./scripts/rebuild.sh
./scripts/update.sh
```

아래는 각각의 조작이며 무조건 순서대로 실행할 목록이 아닙니다. stop·up은 영속 home을 유지하고 `down`은 컨테이너·네트워크를 제거하되 bind-mount된 home을 삭제하지 않습니다. 재빌드는 소스·의존성·CLI 이미지 레이어를 갱신하고 컨테이너를 교체하며 update는 먼저 소스를 fetch·pull합니다. 둘 다 실행 중 턴·작업을 중단할 수 있습니다. home 영속화가 정확히 한 번 전달을 보장하지는 않습니다. 교체 전에 현재 Git SHA·이미지 ID·루트 env·override와 비공개 home 백업을 확보하고 inflight·작업 상태 또는 `/tasks`를 확인하십시오.

애플리케이션을 정리하며 재시작하려면 `/restart`를 보내거나 아래 표식을 만드십시오. 작업은 중단되며 이미지 재빌드·Compose env 갱신은 수행하지 않습니다.

```bash
touch /absolute/path/to/ductor-home/restart-requested
```

롤백은 이전 소스·이미지와 호환되는 배포 설정·home 상태를 함께 복원해야 합니다. 스크립트에는 자동 롤백이 없습니다. 업데이트 전 비공개 백업·정상 이미지를 보관하고 상태 복원 전에 새 쓰기를 멈춘 뒤 Compose·인증·시작을 검증해야 합니다. 이전 Git ref 체크아웃만으로 CLI 버전·실행 데이터가 복원되지는 않습니다.

## 10. 문제 해결

| 증상 | 확인 / 복구 |
| --- | --- |
| 초기 설정 선행 조건·입력 오류 | Docker·Compose v2·Python 설치. 비TTY 토큰·사용자 누락은 exit 2. 파서 도움말만 필요하면 onboard.py --help. |
| /ductor 권한 거부 | 비공개 home·auth·config의 이미지 사용자(온보딩 계정의 `DUCTOR_UID`/`DUCTOR_GID`) 접근 권한 확인. root 소유 0600은 봇이 읽지 못합니다. |
| 브라우저 unhealthy·봇 대기 | 브라우저 로그 확인. 빈 BROWSER_TOKEN은 listen 전 종료시킵니다. 공용 env가 존재하고 두 서비스가 일치해야 합니다. |
| main exit 3·서브 제외 | 선택 matrix·slack·api 의존성 누락: 지원 DUCTOR_EXTRAS를 쉼표로 지정하고 재빌드. main은 기동 실패, 서브는 해당 항목 제외. |
| Telegram 토큰 공백·형식 오류 | 초기 설정은 앞뒤 공백을 제거하고 내부 공백·잘못된 형식을 토큰 표시 없이 거부합니다. 비공개 입력 파일을 고치거나 다시 붙여넣으십시오. --force가 없으면 기존 파일은 유지되므로 비공개 config를 직접 고치거나 백업 후 --force로 재실행하십시오. 토큰·마스킹하지 않은 config를 공유하지 마십시오. |
| Bot online이나 응답 없음 | 실제 토큰·로그인, 사용자·그룹 허용 목록, 원하는 토픽·provider 인증 확인. 그룹만 허용하면 부족합니다. |
| 메시지가 허용 목록에서 거절됨 | Telegram·Matrix·Slack은 실제 허용 목록 거절을 INFO `Allowlist rejection chat_type=... chat_id=... user_id=... topic_id=...`로 기록하며 본문은 넣지 않습니다. 캐시에 남은 같은 채팅/사용자 쌍은 10분에 최대 1회 기록하며, 최대 1,024쌍을 보관하고 오래된 항목을 제거합니다. 각 전송의 허용 목록·mention 정책을 확인하십시오. |
| 현지화된 provider 로그인 안내 | 호스트 저장소에서 응답에 표시된 provider의 `./scripts/login.sh <provider>`를 실행하십시오. 다음 사용자 턴에 인증을 재확인하며 거절된 턴은 CLI를 실행하지 않았습니다. |
| /start 언어가 예상과 다름 | 저장된 config 언어를 확인하십시오. --language/ONBOARD_LANGUAGE와 --force로 저장된 언어를 갱신할 수 있으며 서버 로케일은 새 설치의 기본값입니다. --force가 없으면 기존 config는 유지합니다. |
| 명령 버튼 동작이 예상과 다름 | Telegram의 알려진 ductor 명령은 일반 명령 처리기·필터를 사용하고 모르는 슬래시 명령은 AI로 보냅니다. |
| 서브 질문에 토픽 맥락이 없음 | 비동기 위임 질문은 가능하면 main의 원 활성 토픽 세션을 재개합니다. Named·미확인 background 출처·출처 판별 불가·잠금 60초 초과는 출처 머리말이 있는 전용 세션을 사용하므로 확인된 기록 위치를 참고하십시오. |
| 새 초기 설정 인자가 반영되지 않음 | 기존 파일 유지 때문입니다. config·env 수정 또는 백업 후 의도적으로 --force. --force를 써도 미지정 config 키·그룹 ID는 유지됩니다. override 갱신·extras 재빌드. |
| Gemini 인증되지 않음 표시 | status.sh는 컨테이너의 `GEMINI_API_KEY` / `GOOGLE_API_KEY` 또는 Gemini OAuth 자격 파일을 확인합니다. 선택한 인증 방식이 설정됐는지 확인하고 자격을 붙여 넣지 마십시오. |
| Claude 계정 목록이 바뀌지 않음 | 인증된 슬롯이 둘 이상이면 등록 질문은 기본 Y이며 빈 입력·비TTY는 등록하고 n/no는 등록하지 않습니다. 필요하면 claude_accounts를 수동 설정하십시오. 두 실제 projects 디렉터리는 여전히 자동 병합을 거부하므로 먼저 백업·정리. |
| 비동기 역호출 거부 | IA 턴·headless 발신 비동기의 의도된 거부입니다. 동기 위임 중 main 질문도 거부하므로 최종 보고에 넣으십시오. 비동기 위임 중 필요하면 main에 동기로 물으십시오. |
| 수신 알림 없음·서브 health 중단 | 수신 알림 기본 꺼짐. 켰으면 서브 봇의 원 채팅 접근 필요. 자연 종료 서브는 /agent_stop 후 /agent_start가 필요할 수 있습니다. 한계 문서 참고. |

## 11. 개발과 격리 검증 스크립트

로컬 개발은 봇 home과 별개인 가상환경에 체크아웃 패키지를 설치합니다. Python 3.11 이상, 선택적으로 `uv`, justfile에는 **just 1.42.0 이상**이 필요합니다. 실행은 계속 Compose를 사용합니다. `just check`는 CI가 실패로 치는 검사(타입 검사, 번역 키, 문서 번들, `scripts/tests`, 셸 구문)와 순차 테스트를 실행하고, `just lint`는 ruff를 참고용으로만 실행하며, `just fix`는 파일을 수정합니다. `just test [인자]`는 순차 테스트, `just test-parallel [인자]`는 xdist 선택 실행이며 전체 테스트의 병렬 안전성을 보장하지 않습니다. private `_i18n`·`_docs`·`_scripts`·`_types`·`_test`가 각 검사를 제공합니다.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q ductor_bot
# With uv and just installed:
just check
just test tests/session
```

`scripts/`의 `verify-*` 파일은 저장소 유지보수용 격리 검증 도구로, 일반 설치·사용에는 필요하지 않습니다. 실행 중인 봇의 home·토큰을 대상으로 실행하지 마십시오. `scripts/tests/` 단위 검사는 `python3 -m unittest discover -s scripts/tests`로 실행합니다.

## 12. 문서·알려진 한계·라이선스

| 문서 | 내용 |
| --- | --- |
| [docs/CHANGES.md](docs/CHANGES.md) | 전체 변경점 목록. |
| [docs/config.md](docs/config.md) | 설정 스키마·registry 상세. 오래된 설명과 다르면 코드 기준. |
| [docs/installation.md](docs/installation.md) | 보조 설치 안내. |
| [docs/matrix-setup.md](docs/matrix-setup.md) | Matrix room·로그인·저장·전송 세부 안내. |
| [docs/architecture.md](docs/architecture.md) | 모듈·실행 설계. |
| [docs/system_overview.md](docs/system_overview.md) | 전체 시스템 흐름. |
| [docs/developer_quickstart.md](docs/developer_quickstart.md) | 개발 시작 안내. |
| [docs/automation.md](docs/automation.md) | cron·webhook·백그라운드 작업. |
| [docs/fork_deploy_model.md](docs/fork_deploy_model.md) | 포크 배포·소스 모델. |
| [docs/UPSTREAM.md](docs/UPSTREAM.md) | 수동 upstream 반영 절차. |
| [docs/README.md](docs/README.md) | 문서 색인·모듈 링크. |

알려진 한계는 [CHANGES 7절](docs/CHANGES.md#7-known-limitations)에 기록되어 있습니다. 닫히지 않은 바깥 버튼 표식 뒤의 정상 파일 표식이 최상위로 취급되어 업로드될 수 있습니다. config·인증 경로의 부모 심볼릭 링크를 따라갈 수 있고 링크 검사와 chmod 사이에 경쟁이 남습니다. 자연 종료한 서브는 health만 멈추고 등록이 남아 `/agent_stop` 뒤 `/agent_start`가 필요할 수 있습니다. 파일 읽기·이동 실패와 재시작이 겹치면 같은 결과가 다시 전달될 수 있습니다(정확히 한 번 전달 아님). 에이전트 간 방송이 일부 수신처에서 실패하면 방송 전체를 다시 보내 이미 받은 수신처에 중복될 수 있습니다. 켜 둔 수신 알림은 서브 봇이 원 채팅에 접근하지 못하면 전송에 실패합니다(결과 전달은 별개). 같은 chat ID의 API·Telegram 세션이 공유될 수 있습니다. 이름 있는 세션이 출처이거나 출처를 확인할 수 없거나 세션 잠금을 60초 기다려도 얻지 못하면 질문을 별도 세션에서 처리합니다.

프로젝트는 [MIT](LICENSE) 라이선스로 원저작권 © 2025–2026 PleasePrompto를 유지합니다.
