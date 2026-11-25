import pandas as pd

from sklearn.ensemble import RandomForestClassifier

from ca.models import ChemicalBatch
from dashboards import ml_models


@ml_models.register(
    "ca_rejection_predictor"
)
def rejection_predictor():

    qs = ChemicalBatch.objects.filter(quantity_received__isnull=False).values(
        "quantity_received",
        "status",
    )

    df = pd.DataFrame(qs)

    if len(df) < 20:
        return None

    df["target"] = (
        df["status"] == "REJECTED"
    )

    X = df[
        [
            "quantity_received",
        ]
    ]

    y = df["target"]

    model = RandomForestClassifier(
        n_estimators=200,
        random_state=42,
    )

    model.fit(X, y)

    return model
