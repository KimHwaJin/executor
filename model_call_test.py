"""독립 모델 호출 테스트. agent_config.py나 프로젝트 코드를 불러오지 않습니다.

필요 패키지: langchain-openai (옆의 .env를 사용할 때는 python-dotenv도 필요).
파일 상단 ACTIVE_MODEL, 모델별 이름/파라미터, TIMEOUT_SECONDS를 수정하세요.
API_KEY/BASE_URL이 비어 있으면 환경변수 또는 이 파일 옆의 .env를 사용합니다.
BASE_URL에는 /chat/completions가 아닌 API 기본 주소를 넣으세요.

python model_call_test.py
python model_call_test.py --preset qwen --timeout 120
python model_call_test.py --preset glm --model 서버에등록된모델명
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from time import perf_counter


# 직접 지정할 때만 따옴표 안을 채우세요. 비어 있으면 환경변수/.env 사용.
API_KEY = ""
BASE_URL = ""
# "glm" 또는 "qwen"으로 바꾸면 해당 모델명과 파라미터를 함께 선택합니다.
ACTIVE_MODEL = "glm"
TEST_MESSAGE = "안녕하세요. 한 문장으로 인사해 주세요."
TIMEOUT_SECONDS = 60

# 고객사 서버에 등록된 실제 모델명. 서버의 이름이 다르면 여기를 수정하세요.
GLM_MODEL_NAME = "GLM-5.3"
QWEN_MODEL_NAME = "gaia-Qwen3.6-35B-A3B"

# GLM-5.3: clear_thinking은 이전 대화의 추론 정리 옵션이며 추론 OFF가 아닙니다.
# https://huggingface.co/zai-org/GLM-5.3
# https://recipes.vllm.ai/zai-org/GLM-5.3
GLM_MODEL_PARAMETERS = {
    "temperature": 1.0,
    "top_p": 0.95,
    "reasoning_effort": "low",
    "extra_body": {
        "chat_template_kwargs": {"clear_thinking": True},
    },
    # "max_tokens": 4096,
}

# Qwen3.6-35B-A3B: 공식 non-thinking 권장값으로 시작합니다.
# https://huggingface.co/Qwen/Qwen3.6-35B-A3B
# 추론 ON 테스트: enable_thinking=True, temperature=1.0, top_p=0.95
# 아래 extra_body 형식은 vLLM/SGLang 계열 기준입니다. 서버 지원 여부 확인 필요.
QWEN_MODEL_PARAMETERS = {
    "temperature": 0.7,
    "top_p": 0.8,
    "presence_penalty": 1.5,
    "extra_body": {
        "top_k": 20,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "chat_template_kwargs": {"enable_thinking": False},
    },
    # "max_tokens": 4096,
}


async def call_model(model_name, parameters, api_key, base_url, timeout, prompt) -> None:
    from langchain_openai import ChatOpenAI

    print("실제 모델:", model_name, flush=True)
    print("적용 파라미터:", json.dumps(parameters, ensure_ascii=False), flush=True)
    print(f"타임아웃: {timeout}초 / 재시도: 0회", flush=True)
    print("테스트 문장:", prompt, flush=True)
    model = ChatOpenAI(model=model_name, api_key=api_key, base_url=base_url,
                       timeout=timeout, max_retries=0, **parameters)
    started = perf_counter()
    print("비동기 LLM 요청 시작...", flush=True)
    try:
        result = await model.ainvoke(prompt)
        print("응답:", result.content, flush=True)
    finally:
        print(f"LLM 호출 소요 시간: {perf_counter() - started:.2f}초", flush=True)
        # 이 스크립트가 생성한 SDK 클라이언트의 연결을 정리합니다.
        try:
            client = getattr(model, "root_async_client", None)
            if client is not None:
                await client.close()
        finally:
            client = getattr(model, "root_client", None)
            if client is not None:
                client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preset", choices=("glm", "qwen"), default=ACTIVE_MODEL)
    parser.add_argument("--model", help="선택한 파라미터를 유지하고 서버 모델명만 변경")
    parser.add_argument("--prompt", default=TEST_MESSAGE)
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS)
    args = parser.parse_args()
    if not 0 < args.timeout < float("inf"):
        parser.error("--timeout은 0보다 큰 유한한 숫자여야 합니다.")

    secret = API_KEY or os.environ.get("MODEL_API_KEY", "")
    try:
        env_file = Path(__file__).resolve().with_name(".env")
        if env_file.is_file():
            from dotenv import load_dotenv
            load_dotenv(env_file, override=False)
        secret = API_KEY or os.environ.get("MODEL_API_KEY", "")
        base_url = BASE_URL or os.environ.get("API_BASE_URL", "")
        if not secret or not base_url:
            raise ValueError("파일 상단 API_KEY/BASE_URL 또는 환경변수/.env의 MODEL_API_KEY/API_BASE_URL을 채워 주세요.")
        if args.preset == "glm":
            model_name, parameters = GLM_MODEL_NAME, GLM_MODEL_PARAMETERS
        elif args.preset == "qwen":
            model_name, parameters = QWEN_MODEL_NAME, QWEN_MODEL_PARAMETERS
        else:
            raise ValueError('ACTIVE_MODEL은 "glm" 또는 "qwen"이어야 합니다.')
        asyncio.run(call_model(args.model or model_name, parameters, secret,
                               base_url, args.timeout, args.prompt))
        return 0
    except Exception as exc:
        print("호출 실패. 아래 오류 원인을 확인해 주세요.", file=sys.stderr, flush=True)
        seen = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            message = str(exc).replace(secret, "<API_KEY>") if secret else str(exc)
            print(f"{type(exc).__module__}.{type(exc).__name__}: {message}", file=sys.stderr, flush=True)
            exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
