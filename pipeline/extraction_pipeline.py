import gemini_service
from data_to_docx.extract_json import extract_context_from_markdown


def run_extraction_pipeline(image_items: list[tuple[str, bytes]]) -> tuple[str, dict]:
    markdown = gemini_service.run_extraction(image_items)
    context = extract_context_from_markdown(markdown)
    return markdown, context
