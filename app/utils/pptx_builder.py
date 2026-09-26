import io
from pptx import Presentation
from app.utils.r2 import upload_file_to_r2

PRESENTATION_TOOL = {
    "name": "generate_presentation",
    "description": "Generates a PowerPoint presentation (.pptx) file and returns a download link when requested by the user.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The presentation main title"},
            "subtitle": {"type": "string", "description": "Presentation subtitle or author context"},
            "slides": {
                "type": "array",
                "description": "List of slide topics and content",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Slide heading"},
                        "content": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Bullet points for the slide",
                        },
                    },
                    "required": ["title", "content"],
                },
            },
        },
        "required": ["title", "slides"],
    },
}


def create_presentation_file(title: str, subtitle: str, slides_data: list) -> str:
    prs = Presentation()

    title_slide_layout = prs.slide_layouts[0]
    slide = prs.slides.add_slide(title_slide_layout)
    title_shape = slide.shapes.title
    subtitle_shape = slide.placeholders[1]
    title_shape.text = title
    subtitle_shape.text = subtitle or "Kerneu Group"

    bullet_slide_layout = prs.slide_layouts[1]
    for s_data in slides_data:
        slide = prs.slides.add_slide(bullet_slide_layout)
        shapes = slide.shapes
        title_shape = shapes.title
        body_shape = shapes.placeholders[1]

        title_shape.text = s_data.get("title", "")
        tf = body_shape.text_frame

        content_items = s_data.get("content", [])
        if content_items:
            tf.text = content_items[0]
            for item in content_items[1:]:
                p = tf.add_paragraph()
                p.text = item

    stream = io.BytesIO()
    prs.save(stream)
    file_bytes = stream.getvalue()

    safe_title = "".join(c if c.isalnum() else "_" for c in title)[:20]
    filename = f"{safe_title}.pptx"

    return upload_file_to_r2(
        file_bytes=file_bytes,
        original_filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        folder="presentations",
    )