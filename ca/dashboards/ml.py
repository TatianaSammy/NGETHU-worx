import pandas as pd
from sklearn.ensemble import RandomForestClassifier

from ca.models import (
    ChemicalBatch,
    BatchTestResult,
)

from dashboards.registry import registry


@registry.ml.register(
    key="ca.batch_failure_prediction"
)
def batch_failure_prediction():

    rows = []

    for batch in ChemicalBatch.objects.filter(quantity_received__isnull=False).prefetch_related(
        "test_results"
    ):
        rows.append({
            "quantity":
                batch.quantity_received,
            "approved":
                batch.status == "APPROVED",
        })

    if len(rows) < 20:
        return {
            "trained": False,
        }

    df = pd.DataFrame(rows)

    X = df[["quantity"]]
    y = df["approved"]

    model = RandomForestClassifier(
        n_estimators=100
    )

    model.fit(X, y)

    return {
        "trained": True,
        "samples": len(df),
        "accuracy":
            model.score(X, y),
    }
