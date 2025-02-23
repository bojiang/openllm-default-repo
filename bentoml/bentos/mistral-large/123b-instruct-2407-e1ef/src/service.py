from __future__ import annotations

import base64, io, os, traceback, typing, argparse, asyncio, logging
import bentoml, yaml, fastapi, PIL.Image, typing_extensions, annotated_types

import fastapi.staticfiles as staticfiles, fastapi.responses as responses

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

STATIC_DIR = os.path.join(os.path.dirname(__file__), 'ui')

with open(os.path.join(os.path.dirname(__file__), 'openllm_config.yaml')) as f:
    PARAMETERS = yaml.safe_load(f)

ENGINE_CONFIG = PARAMETERS.get('engine_config', {})
SERVICE_CONFIG = PARAMETERS.get('service_config', {})
SERVER_CONFIG = PARAMETERS.get('server_config', {})
IMAGE_CONFIG = PARAMETERS.get('build', {})

SUPPORTS_VISION = PARAMETERS.get('vision', False)
SUPPORTS_EMBEDDINGS = PARAMETERS.get('embeddings', False)
MAX_TOKENS = min(ENGINE_CONFIG.get('max_model_len', 2048), 4096)

openai_api_app = fastapi.FastAPI()
ui_app = fastapi.FastAPI()
ui_app.mount('/static', staticfiles.StaticFiles(directory=STATIC_DIR), name='static')


@ui_app.get('/')
async def serve_chat_html():
    return responses.FileResponse(os.path.join(STATIC_DIR, 'chat.html'))


@ui_app.get('/{full_path:path}')
async def catch_all(full_path: str):
    file_path = os.path.join(STATIC_DIR, full_path)
    if os.path.exists(file_path):
        return responses.FileResponse(file_path)
    return responses.FileResponse(os.path.join(STATIC_DIR, 'chat.html'))


IMAGE = bentoml.images.PythonImage(python_version='3.11')
if len((PRE_COMMANDS := IMAGE_CONFIG.get('pre', []))) > 0:
    for cmd in PRE_COMMANDS:
        IMAGE = IMAGE.run(cmd)
IMAGE = IMAGE.requirements_file('requirements.txt')
if len((REQUIREMENTS_TXT := PARAMETERS.get('requirements', []))) > 0:
    IMAGE = IMAGE.python_packages(*REQUIREMENTS_TXT)
if len((POST_COMMANDS := IMAGE_CONFIG.get('post', []))) > 0:
    for cmd in POST_COMMANDS:
        IMAGE = IMAGE.run(cmd)


@bentoml.asgi_app(openai_api_app, path='/v1')
@bentoml.asgi_app(ui_app, path='/chat')
@bentoml.service(
    **SERVICE_CONFIG, image=IMAGE, labels=dict(generator='openllm', owner='bentoml-team', type='prebuilt')
)
class VLLM:
    model_id = ENGINE_CONFIG['model']
    model = bentoml.models.HuggingFaceModel(model_id, exclude=[*IMAGE_CONFIG.get('exclude', []), '*.pth', '*.pt'])

    def __init__(self) -> None:
        from openai import AsyncOpenAI
        from vllm import AsyncEngineArgs, AsyncLLMEngine
        import vllm.entrypoints.openai.api_server as vllm_api_server

        OPENAI_ENDPOINTS = [
            ['/models', vllm_api_server.show_available_models, ['GET']],
            ['/chat/completions', vllm_api_server.create_chat_completion, ['POST']],
        ]
        if SUPPORTS_EMBEDDINGS:
            OPENAI_ENDPOINTS.append(['/embeddings', vllm_api_server.create_embedding, ['POST']])

        for route, endpoint, methods in OPENAI_ENDPOINTS:
            openai_api_app.add_api_route(path=route, endpoint=endpoint, methods=methods, include_in_schema=True)

        ENGINE_ARGS = AsyncEngineArgs(**dict(ENGINE_CONFIG, model=self.model))
        engine = AsyncLLMEngine.from_engine_args(ENGINE_ARGS)

        model_config = engine.engine.get_model_config()
        args = argparse.Namespace()
        args.model = self.model
        args.disable_log_requests = True
        args.max_log_len = 1000
        args.response_role = 'assistant'
        args.served_model_name = [self.model_id]
        args.chat_template = None
        args.chat_template_content_format = 'auto'
        args.lora_modules = None
        args.prompt_adapters = None
        args.request_logger = None
        args.disable_log_stats = True
        args.return_tokens_as_token_ids = False
        args.enable_tool_call_parser = False
        args.enable_auto_tool_choice = False
        args.tool_call_parser = None
        args.enable_prompt_tokens_details = False
        args.enable_reasoning = False
        args.reasoning_parser = None
        for key, value in SERVER_CONFIG.items():
            setattr(args, key, value)

        asyncio.create_task(vllm_api_server.init_app_state(engine, model_config, openai_api_app.state, args))
        self.client = AsyncOpenAI(base_url='http://127.0.0.1:3000/v1', api_key='dummy')

    if not SUPPORTS_EMBEDDINGS:

        @bentoml.api
        async def generate(
            self,
            prompt: str = 'Who are you? Please respond in pirate speak!',
            max_tokens: typing.Annotated[int, annotated_types.Ge(128), annotated_types.Le(MAX_TOKENS)] = MAX_TOKENS,
        ) -> typing.AsyncGenerator[str, None]:
            try:
                completion = await self.client.chat.completions.create(
                    model=self.model_id,
                    messages=[dict(role='user', content=[dict(type='text', text=prompt)])],
                    stream=True,
                    max_tokens=max_tokens,
                )
                async for chunk in completion:
                    yield chunk.choices[0].delta.content or ''
            except Exception:
                logger.error(traceback.format_exc())
                yield 'Internal error found. Check server logs for more information'
                return

        if SUPPORTS_VISION:

            @bentoml.api
            async def sights(
                self,
                prompt: str = 'Who are you? Please respond in pirate speak!',
                image: typing.Optional[PIL.Image.Image] = None,
                max_tokens: typing_extensions.Annotated[
                    int, annotated_types.Ge(128), annotated_types.Le(MAX_TOKENS)
                ] = MAX_TOKENS,
            ) -> typing.AsyncGenerator[str, None]:
                if image:
                    buffered = io.BytesIO()
                    image.save(buffered, format='PNG')
                    img_str = base64.b64encode(buffered.getvalue()).decode()
                    buffered.close()
                    image_url = f'data:image/png;base64,{img_str}'
                    content = [dict(type='image_url', image_url=dict(url=image_url)), dict(type='text', text=prompt)]
                else:
                    content = [dict(type='text', text=prompt)]

                try:
                    completion = await self.client.chat.completions.create(
                        model=self.model_id,
                        messages=[dict(role='user', content=content)],
                        stream=True,
                        max_tokens=max_tokens,
                    )
                    async for chunk in completion:
                        yield chunk.choices[0].delta.content or ''
                except Exception:
                    logger.error(traceback.format_exc())
                    yield 'Internal error found. Check server logs for more information'
                    return
