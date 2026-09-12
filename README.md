# YoDb

YoDb is an AI-oriented logical data layer. This first increment defines a
backend-neutral schema model: datasets and typed records, relationships with
edge fields, derived indexes such as vector embeddings, and field-level
presence/default semantics.

```python
from yodb import DatasetSpec, FieldSpec, FieldType, IndexKind, IndexSpec

documents = DatasetSpec(
    name="documents",
    fields={
        "id": FieldSpec(FieldType.ID),
        "content": FieldSpec(FieldType.TEXT, searchable=True),
    },
    indexes=(
        IndexSpec(
            name="content_embedding",
            kind=IndexKind.VECTOR,
            source_field="content",
            embedding_model="text-embedding-3-large",
            dimensions=3072,
            distance="cosine",
        ),
    ),
)
```

Run the tests with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
