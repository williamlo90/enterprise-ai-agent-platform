"""Public validation boundary shared by model consumers."""
from enterprise_ai_platform.modules.models.domain import vectors

def validate_embeddings(value,expected_count):return vectors(value,expected_count)
