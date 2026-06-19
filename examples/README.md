# Examples

## Custom connectors

`connectors/custom_connector_template.py` is a starting point for pulling data
from a source Slashbooks doesn't ship a connector for. Copy it into your
company's books folder under `ingestion/custom/`, adapt the fetch step to your
provider's API, then bring the data in:

```sh
python ingestion/custom/my_connector.py out.json
books ingest out.json --entity . --source my-connector
```

The connector only fetches and normalizes data. The deterministic importer does
the accounting, so trusted counterparties auto-post, unknown ones go to the
review queue, and re-running is idempotent.

See [docs/connectors.md](../docs/connectors.md) for the full picture.
