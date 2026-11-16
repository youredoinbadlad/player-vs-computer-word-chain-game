"""영어 끝말잇기 웹 서버 (Flask)

콘솔 버전의 get_word_info / find_computer_word 로직을 그대로 사용하고,
input() / print() 대신 HTTP JSON API로 게임을 진행한다.

로컬 실행:  python app.py   ->  http://localhost:5000
배포 실행:  gunicorn app:app --workers 1 --threads 8 --timeout 60
(게임 상태를 서버 메모리에 두므로 worker는 1개로 유지한다)
"""
import os
import random
import secrets
import threading
import time

import requests
from flask import Flask, jsonify, render_template, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# ---------------------------------------------------------------- 설정
DIFFICULTIES = {
    "1": ("매우 쉬움", 5),
    "2": ("쉬움", 10),
    "3": ("중간", 25),
    "4": ("어려움", 50),
    "5": ("매우 어려움", 100),
}
MAX_WRONG = 5            # 최대 허용 틀린 횟수
START_WORD = "apple"     # 컴퓨터 시작 단어
MAX_COMPUTER_TRIES = 12  # 컴퓨터가 단어를 찾을 때 사전 조회 최대 횟수
GAME_TTL = 60 * 60       # 1시간 동안 움직임이 없으면 게임 삭제


# ---------------------------------------------------------------- 사전 API
_WORD_CACHE = {}
_CACHE_MAX = 5000


def get_word_info(word):
    """
    Free Dictionary API를 호출하여 영단어 존재 여부 및 뜻 가져오기
    반환값: (결과_상태_코드, 뜻_또는_메시지)
    - 200: 정상 단어
    - 404: 사전에 없는 단어
    - 500: API/네트워크 오류 (캐시하지 않음)
    """
    clean_word = word.strip().lower()
    if clean_word in _WORD_CACHE:
        return _WORD_CACHE[clean_word]

    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{clean_word}"

    try:
        response = requests.get(url, timeout=5)

        if response.status_code == 404:
            result = (404, "사전에 존재하지 않는 영단어입니다.")

        elif response.status_code == 200:
            result = (200, "Definition available in English dictionary.")
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                found = False
                for meaning in data[0].get("meanings", []):
                    for def_obj in meaning.get("definitions", []):
                        if "definition" in def_obj:
                            result = (200, def_obj["definition"])
                            found = True
                            break
                    if found:
                        break
        else:
            return 500, f"HTTP 오류 발생 (코드: {response.status_code})"

    except requests.exceptions.Timeout:
        return 500, "네트워크 요청 시간이 초과되었습니다."
    except Exception as e:
        return 500, f"API 통신 오류: {e}"

    if len(_WORD_CACHE) >= _CACHE_MAX:
        _WORD_CACHE.clear()
    _WORD_CACHE[clean_word] = result
    return result


def find_computer_word(start_char, used_words):
    """
    Datamuse API를 이용해 start_char로 시작하는 실제 영단어를 실시간 검색
    """
    url = f"https://api.datamuse.com/words?sp={start_char.lower()}*&max=200"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            # 상위(흔한) 단어 위주로 섞어서 너무 어려운 단어가 나오지 않게 한다
            words_data = response.json()[:80]
            random.shuffle(words_data)

            tries = 0
            for item in words_data:
                word = item["word"].lower()
                if (
                    word.isascii()
                    and word.isalpha()
                    and len(word) >= 2
                    and word not in used_words
                ):
                    tries += 1
                    if tries > MAX_COMPUTER_TRIES:
                        break
                    status, meaning = get_word_info(word)
                    if status == 200:
                        return word, meaning
    except Exception:
        pass
    return None, None


# ---------------------------------------------------------------- 게임 상태
GAMES = {}
GAMES_LOCK = threading.Lock()


def _purge_old_games():
    now = time.time()
    with GAMES_LOCK:
        for gid in [k for k, g in GAMES.items() if now - g["touched"] > GAME_TTL]:
            del GAMES[gid]


def _get_game():
    gid = session.get("gid")
    with GAMES_LOCK:
        game = GAMES.get(gid)
    if game:
        game["touched"] = time.time()
    return game


def public_state(g):
    return {
        "correct": g["correct"],
        "wrong": g["wrong"],
        "target": g["target"],
        "max_wrong": MAX_WRONG,
        "next_letter": g["current"][-1].upper(),
    }


def _finish(g, reason):
    g["over"] = True
    return {"finished": reason, "words": list(g["used"])}


def _reply(g, out):
    if not g["over"] and g["wrong"] >= MAX_WRONG:
        out.update(_finish(g, "lose"))
    out["state"] = public_state(g)
    return out


def _handle_word(g, raw):
    """플레이어가 낸 단어를 검사하고 컴퓨터의 다음 단어까지 계산한다."""
    word = str(raw).strip().lower()
    last_char = g["current"][-1].lower()

    # 입력 형식 오류 (기회 차감 없음)
    if not word or len(word) > 45 or not (word.isascii() and word.isalpha()):
        return _reply(g, {"result": "invalid", "msg": "영어 알파벳으로 된 한 단어만 입력해 주세요."})

    # 1. 중복 단어 검사 (기회 차감 없음)
    if word in g["used"]:
        return _reply(g, {"result": "duplicate", "msg": "이미 사용된 단어예요. 다른 단어를 입력해 주세요."})

    # 2. 첫 글자 일치 검사 (기회 차감)
    if not word.startswith(last_char):
        g["wrong"] += 1
        return _reply(g, {
            "result": "wrong_char",
            "msg": f"'{last_char.upper()}'(으)로 시작하는 단어를 입력해 주세요. 기회가 1개 줄었어요.",
        })

    # 3. 단어 유효성 및 API 상태 검사
    status, meaning = get_word_info(word)

    if status == 404:  # 사전에 없음 -> 기회 차감
        g["wrong"] += 1
        return _reply(g, {"result": "not_found", "msg": "사전에 없는 영단어예요. 기회가 1개 줄었어요."})
    if status == 500:  # API 오류 -> 기회 차감 없음
        return _reply(g, {
            "result": "api_error",
            "msg": "사전 서버에 연결하지 못했어요. 기회는 줄지 않았어요. 잠시 후 다시 제출해 주세요.",
        })

    # 플레이어 정답 처리
    g["used"].append(word)
    g["correct"] += 1
    g["current"] = word
    out = {"result": "ok", "word": word, "meaning": meaning}

    # 목표 달성 시 승리
    if g["correct"] >= g["target"]:
        out.update(_finish(g, "win_target"))
        return _reply(g, out)

    # 컴퓨터 순서
    comp_word, comp_meaning = find_computer_word(word[-1], g["used"])
    if comp_word:
        g["used"].append(comp_word)
        g["current"] = comp_word
        out.update(computer_word=comp_word, computer_meaning=comp_meaning)
    else:
        out.update(_finish(g, "win_stuck"))  # 컴퓨터가 이을 단어를 못 찾음
    return _reply(g, out)


# ---------------------------------------------------------------- 라우트
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.post("/api/start")
def api_start():
    _purge_old_games()
    level = str((request.get_json(silent=True) or {}).get("level", ""))
    if level not in DIFFICULTIES:
        return jsonify(error="난이도를 1~5 중에서 선택해 주세요."), 400

    name, target = DIFFICULTIES[level]
    status, meaning = get_word_info(START_WORD)
    if status != 200:
        meaning = ""

    game = {
        "target": target,
        "correct": 0,
        "wrong": 0,
        "used": [START_WORD],
        "current": START_WORD,
        "over": False,
        "touched": time.time(),
        "lock": threading.Lock(),
    }
    gid = secrets.token_urlsafe(16)
    with GAMES_LOCK:
        GAMES.pop(session.get("gid"), None)
        GAMES[gid] = game
    session["gid"] = gid

    return jsonify(level_name=name, word=START_WORD, meaning=meaning, state=public_state(game))


@app.post("/api/submit")
def api_submit():
    game = _get_game()
    if not game:
        return jsonify(error="진행 중인 게임이 없어요. 난이도를 선택해 새로 시작해 주세요."), 404
    if game["over"]:
        return jsonify(error="이미 끝난 게임이에요. 새 게임을 시작해 주세요."), 409
    if not game["lock"].acquire(blocking=False):
        return jsonify(error="이전 단어를 처리하는 중이에요. 잠시 기다려 주세요."), 429
    try:
        raw = (request.get_json(silent=True) or {}).get("word", "")
        return jsonify(_handle_word(game, raw))
    finally:
        game["lock"].release()


@app.post("/api/quit")
def api_quit():
    game = _get_game()
    if not game:
        return jsonify(error="진행 중인 게임이 없어요."), 404
    with game["lock"]:
        out = {"finished": "quit", "words": list(game["used"])}
        game["over"] = True
        out["state"] = public_state(game)
    return jsonify(out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
