import time
from typing import Any

from google import genai
from google.genai import types

from app.utils.docx_builder import create_document_file
from app.utils.image_builder import generate_and_save_image
from app.utils.pptx_builder import create_presentation_file
from app.utils.xlsx_builder import create_spreadsheet_file
from .base import ALL_TOOLS, Attachment, GenerationResult, ModelAdapter

_PRO_VALID_LEVELS = {"low", "high"}
_FLASH_VALID_LEVELS = {"minimal", "low", "medium", "high"}
_PRO_FALLBACK_LEVEL = "low"


class GeminiAdapter(ModelAdapter):
    def __init__(
        self,
        api_key: str,
        openai_api_key: str | None = None,
        model: str = "gemini-3.6-flash",
        thinking_level: str = "minimal",
    ):
        self.name = model
        self.client = genai.Client(api_key=api_key)
        self.openai_api_key = openai_api_key or api_key
        self.model = model
        self.thinking_level = self._resolve_thinking_level(model, thinking_level)

    @staticmethod
    def _resolve_thinking_level(model: str, requested_level: str) -> str:
        is_pro = "pro" in model.lower()
        valid_levels = _PRO_VALID_LEVELS if is_pro else _FLASH_VALID_LEVELS
        if requested_level.lower() in valid_levels:
            return requested_level.lower()
        return _PRO_FALLBACK_LEVEL if is_pro else requested_level.lower()

    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        web_search: bool = False,
        max_tokens: int = 2048,
        attachments: list[Attachment] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerationResult:
        started = time.monotonic()

        tool_list = []
        if web_search:
            tool_list.append(types.Tool(google_search=types.GoogleSearch()))

        active_tools = tools if tools is not None else ALL_TOOLS
        if active_tools:
            func_decls = [
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t["description"],
                    parameters=t["parameters"],
                )
                for t in active_tools
            ]
            tool_list.append(types.Tool(function_declarations=func_decls))

        config_kwargs: dict = {
            "max_output_tokens": max_tokens,
            "thinking_config": types.ThinkingConfig(thinking_level=self.thinking_level),
        }
        # ВАЖНО: "tools" добавляется в конфиг, ТОЛЬКО если там реально что-то
        # есть. Gemini жёстко запрещает связку response_mime_type=
        # "application/json" (json_mode, см. ниже) с любыми tools/function
        # declarations в одном запросе — даже с пустым списком деклараций
        # это может быть отклонено. Раньше "tools" передавался всегда
        # (пусть даже как пустой), из-за чего json_mode-вызовы (классификатор)
        # падали на каждом обращении.
        if tool_list:
            config_kwargs["tools"] = tool_list
        if system:
            config_kwargs["system_instruction"] = system
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=self._build_contents(prompt, attachments),
            config=types.GenerateContentConfig(**config_kwargs),
        )

        latency_ms = int((time.monotonic() - started) * 1000)
        output_text = response.text or ""

        # Обработка вызова функций в Gemini
        if response.function_calls:
            for call in response.function_calls:
                args = call.args or {}
                if call.name == "generate_presentation":
                    file_url = create_presentation_file(
                        title=args.get("title", "Презентация"),
                        subtitle=args.get("subtitle", ""),
                        slides_data=args.get("slides", []),
                    )
                    output_text = (
                        f"📊 Готово! Я сформировал презентацию «**{args.get('title')}**».\n\n"
                        f"[📥 Скачать презентацию (.pptx)]({file_url})"
                    )
                elif call.name == "generate_document":
                    try:
                        file_url = create_document_file(
                            title=args.get("title", "Документ"),
                            sections=args.get("sections", []),
                        )
                        output_text = (
                            f"📄 Готово! Я сформировал документ «**{args.get('title')}**».\n\n"
                            f"[📥 Скачать документ (.docx)]({file_url})"
                        )
                    except Exception as e:
                        output_text = f"⚠️ Не удалось сформировать документ. Ошибка: {str(e)}"
                elif call.name == "generate_spreadsheet":
                    try:
                        file_url = create_spreadsheet_file(
                            filename=args.get("filename", "Таблица"),
                            sheets=args.get("sheets", []),
                        )
                        output_text = (
                            f"📈 Готово! Я сформировал таблицу «**{args.get('filename')}**».\n\n"
                            f"[📥 Скачать таблицу (.xlsx)]({file_url})"
                        )
                    except Exception as e:
                        output_text = f"⚠️ Не удалось сформировать таблицу. Ошибка: {str(e)}"
                elif call.name == "generate_image":
                    try:
                        img_url = await generate_and_save_image(
                            prompt=args.get("prompt", prompt),
                            size=args.get("size", "1024x1024"),
                        )
                        output_text = f"🎨 Вот изображение по вашему запросу:\n\n![Сгенерированное изображение]({img_url})"
                    except Exception as e:
                        output_text = f"⚠️ Не удалось сгенерировать изображение. Ошибка: {str(e)}"

        usage = response.usage_metadata
        tokens_in = usage.prompt_token_count or 0 if usage else 0
        tokens_out = usage.candidates_token_count or 0 if usage else 0

        return GenerationResult(
            text=output_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            raw={"model": self.model},
        )

    @staticmethod
    def _build_contents(prompt: str, attachments: list[Attachment] | None):
        if not attachments:
            return prompt

        parts = [
            types.Part.from_bytes(data=att.data, mime_type=att.mime_type) for att in attachments
        ]
        parts.append(types.Part.from_text(text=prompt))
        return parts
