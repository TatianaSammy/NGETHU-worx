from datetime import time
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from qc.models import (
    SamplingSchedule,
    SamplingTime,
    TreatedWaterResult,
    WaterAnalysis,
)
from qc.services.result_card_service import AnalysisCard, ResultCell, ResultGroup, ResultRow
from qc.services.wqi_service import (
    calculate_wqi_for_card,
    calculate_wqi_for_readings,
    classify_wqi,
    compute_parameter_qi,
    get_dashboard_wqi_summary,
    get_overall_wqi,
    get_plant_wqi,
    load_plant_limits,
    invalidate_plant_limit_cache,
)
from t.models import TreatmentPlant, TreatedWaterLimit
from parameters.models import PhysicochemicalParameter

User = get_user_model()


class WaterQualityIndexTests(TestCase):
    def test_wqi_formula_exact_match_metricgate(self):
        """Verify calculation precisely reproduces the NSF / MetricGate WQI benchmark.

        Limits are passed explicitly in the readings dict, simulating what
        get_plant_wqi() extracts from TreatedWaterLimit rows.

        pH: 7.20 (ideal 7.0, limit 8.5) -> q_i = 13.33
        Dissolved Oxygen: 8.50 (ideal 14.6, limit 5.0) -> q_i = 63.54
        BOD: 2.00 (ideal 0.0, limit 5.0) -> q_i = 40.00
        Temp Change: 1.50 (ideal 0.0, limit 5.0) -> q_i = 30.00
        Turbidity: 5.00 (ideal 0.0, limit 5.0) -> q_i = 100.00
        TDS: 250.00 (ideal 0.0, limit 500.0) -> q_i = 50.00

        Expected WQI: 52.60 (Poor)
        """
        readings = [
            {"name": "pH", "value": 7.20, "min_limit": 6.5, "max_limit": 8.5},
            {"name": "Dissolved Oxygen", "value": 8.50, "min_limit": 5.0, "max_limit": 14.6},
            {"name": "BOD", "value": 2.00, "min_limit": 0.0, "max_limit": 5.0},
            {"name": "Temperature Change", "value": 1.50, "min_limit": 0.0, "max_limit": 5.0},
            {"name": "Turbidity", "value": 5.00, "min_limit": 0.0, "max_limit": 5.0},
            {"name": "Total Dissolved Solids", "value": 250.00, "min_limit": 0.0, "max_limit": 500.0},
        ]
        result = calculate_wqi_for_readings(readings)
        self.assertEqual(result["score"], 52.60)
        self.assertEqual(result["tier_label"], "Poor")
        self.assertEqual(result["tier_key"], "poor")
        self.assertEqual(result["concerns_count"], 2)

        concern_names = [p["name"] for p in result["parameters_of_concern"]]
        self.assertIn("Turbidity", concern_names)
        self.assertIn("Dissolved Oxygen", concern_names)

        # Best parameter: pH (13.33)
        self.assertEqual(result["best_parameter"]["name"], "pH")
        self.assertEqual(result["best_parameter"]["qi"], 13.33)

        # Worst parameter: Turbidity (100.00)
        self.assertEqual(result["worst_parameter"]["name"], "Turbidity")
        self.assertEqual(result["worst_parameter"]["qi"], 100.00)

    def test_wqi_db_limit_lookup(self):
        """Verify WQI loads limits from TreatedWaterLimit in the t app (no hardcoded values)."""
        plant = TreatmentPlant.objects.create(name="Test Plant DB", code="TPT_DB")

        # Create parameters and their limits in the DB (simulating admin setup)
        ph_param = PhysicochemicalParameter.objects.create(name="pH_TestDB", unit="pH units")
        turb_param = PhysicochemicalParameter.objects.create(name="Turbidity_TestDB", unit="NTU")

        TreatedWaterLimit.objects.create(
            treatment_plant=plant, parameter=ph_param,
            min_value=Decimal("6.5"), max_value=Decimal("8.5"),
        )
        TreatedWaterLimit.objects.create(
            treatment_plant=plant, parameter=turb_param,
            min_value=None, max_value=Decimal("5.0"),
        )

        # Invalidate cache to ensure fresh load from DB
        invalidate_plant_limit_cache(plant.pk)

        # Load limits — should return 2 entries sourced from DB
        limits = load_plant_limits(plant.pk)
        self.assertIn(2, [len(limits), len(limits)])  # at least our 2 params

        ph_key = next((k for k in limits if "ph" in k.lower()), None)
        turb_key = next((k for k in limits if "turbidity" in k.lower()), None)
        self.assertIsNotNone(ph_key, "pH limit not found in cache")
        self.assertIsNotNone(turb_key, "Turbidity limit not found in cache")
        self.assertEqual(limits[ph_key]["min"], 6.5)
        self.assertEqual(limits[ph_key]["max"], 8.5)
        self.assertIsNone(limits[turb_key]["min"])
        self.assertEqual(limits[turb_key]["max"], 5.0)

        # WQI calculation should use DB limits (no hardcoded values)
        # Provide readings without min/max — service must resolve from plant_limits cache
        readings = [
            {"name": "pH_TestDB", "value": 7.5, "min_limit": None, "max_limit": None},
            {"name": "Turbidity_TestDB", "value": 2.5, "min_limit": None, "max_limit": None},
        ]
        result = calculate_wqi_for_readings(readings, plant_limits=limits)
        # Both parameters have DB limits so WQI should be computed
        self.assertIsNotNone(result["score"], "WQI score should be computed when limits are in DB")
        self.assertGreater(result["parameters_evaluated"], 0)

    def test_wqi_no_limits_returns_no_data(self):
        """Parameters with no limits in DB are excluded from WQI (no hardcoded fallback)."""
        readings = [
            # min_limit and max_limit both None → no DB entry → must be skipped
            {"name": "UnknownParamXYZ", "value": 42.0, "min_limit": None, "max_limit": None},
        ]
        result = calculate_wqi_for_readings(readings)
        self.assertIsNone(result["score"], "Should return No Data when no limits available")
        self.assertEqual(result["tier_key"], "nodata")

    def test_wqi_classification_tiers(self):
        """Test the standard 5-tier classification thresholds."""
        self.assertEqual(classify_wqi(0.0)["tier_label"], "Excellent")
        self.assertEqual(classify_wqi(25.0)["tier_label"], "Excellent")
        self.assertEqual(classify_wqi(25.01)["tier_label"], "Good")
        self.assertEqual(classify_wqi(50.0)["tier_label"], "Good")
        self.assertEqual(classify_wqi(50.01)["tier_label"], "Poor")
        self.assertEqual(classify_wqi(75.0)["tier_label"], "Poor")
        self.assertEqual(classify_wqi(75.01)["tier_label"], "Very Poor")
        self.assertEqual(classify_wqi(100.0)["tier_label"], "Very Poor")
        self.assertEqual(classify_wqi(100.01)["tier_label"], "Unsuitable for Drinking")
        self.assertEqual(classify_wqi(150.0)["tier_key"], "unsuitable")
        self.assertEqual(classify_wqi(None)["tier_label"], "No Data")

    def test_parameter_qi_computation(self):
        """Test individual parameter rating computations (limits passed explicitly as DB would supply)."""
        # Turbidity in range
        qi, s, ideal, cl = compute_parameter_qi("Turbidity", 1.0, max_limit=5.0)
        self.assertEqual(qi, 20.0)
        self.assertEqual(cl["tier_label"], "Excellent")

        # Turbidity at limit
        qi, s, ideal, cl = compute_parameter_qi("Turbidity", 5.0, max_limit=5.0)
        self.assertEqual(qi, 100.0)
        self.assertEqual(cl["tier_label"], "Very Poor")

        # Turbidity exceedance
        qi, s, ideal, cl = compute_parameter_qi("Turbidity", 10.0, max_limit=5.0)
        self.assertEqual(qi, 200.0)
        self.assertEqual(cl["tier_label"], "Unsuitable for Drinking")

        # pH at ideal
        qi, s, ideal, cl = compute_parameter_qi("pH", 7.0, max_limit=8.5, min_limit=6.5)
        self.assertEqual(qi, 0.0)
        self.assertEqual(cl["tier_label"], "Excellent")

        # Parameter with no limits → returns None (no hardcoded fallback)
        qi, s, ideal, cl = compute_parameter_qi("SomeParam", 10.0, min_limit=None, max_limit=None)
        self.assertIsNone(qi)

    def test_calculate_wqi_for_card(self):
        """Test cycle WQI calculation from an AnalysisCard.

        Cells carry min/max from TreatedWaterLimit (populated by ResultCardService).
        """
        cell1 = ResultCell(
            result=None,
            parameter="Turbidity",
            unit="NTU",
            value=Decimal("2.5"),
            minimum=Decimal("0.0"),
            maximum=Decimal("5.0"),
            entered=True,
            abnormal=False,
            display_value="2.5",
            display_limit="0.0 - 5.0",
        )
        cell2 = ResultCell(
            result=None,
            parameter="pH",
            unit="pH",
            value=Decimal("7.2"),
            minimum=Decimal("6.5"),
            maximum=Decimal("8.5"),
            entered=True,
            abnormal=False,
            display_value="7.2",
            display_limit="6.5 - 8.5",
        )
        row = ResultRow(name="Phase 1", results={"Turbidity": cell1, "pH": cell2})
        group = ResultGroup(title="TREATED WATER", water_type="treated", rows=[row])
        card = AnalysisCard(sampling_time=None, time_label="08:00", groups=[group])

        wqi = calculate_wqi_for_card(card)
        self.assertIsNotNone(wqi["score"])
        self.assertEqual(wqi["parameters_evaluated"], 2)
        self.assertIn(wqi["tier_label"], ["Excellent", "Good", "Poor", "Very Poor", "Unsuitable for Drinking"])

    def test_plant_scoping_permissions(self):
        """Verify non-superusers are strictly scoped to their assigned plant."""
        plant_a = TreatmentPlant.objects.create(name="Plant Alpha", code="PL_ALPHA")
        plant_b = TreatmentPlant.objects.create(name="Plant Beta", code="PL_BETA")

        non_super = User.objects.create_user(
            username="NCWSC99001",
            email="alpha@nairobiwater.co.ke",
            password="TestPassword123!",
            assigned_treatment_plant=plant_a,
        )
        super_user = User.objects.create_superuser(
            username="NCWSC99002",
            email="admin@nairobiwater.co.ke",
            password="TestPassword123!",
        )

        # Non-super user summary
        scoped_summary = get_dashboard_wqi_summary(non_super, selected_plant=plant_b)
        self.assertTrue(scoped_summary["is_scoped"])
        self.assertFalse(scoped_summary["is_superuser"])
        # Strictly locked to plant_a despite selected_plant=plant_b
        self.assertEqual(scoped_summary["plant_scoped"]["plant_id"], plant_a.pk)

        # Superuser summary
        super_summary = get_dashboard_wqi_summary(super_user, selected_plant=plant_b)
        self.assertFalse(super_summary["is_scoped"])
        self.assertTrue(super_summary["is_superuser"])
        self.assertEqual(super_summary["plant_scoped"]["plant_id"], plant_b.pk)
        self.assertIsNotNone(super_summary["overall"])

    def test_wqi_advanced_multi_model_and_attributes(self):
        """Test multi-model evaluation, eclipsing, suitability matrix, and remediation priorities."""
        readings = [
            {"name": "pH", "value": 7.20, "min_limit": 6.5, "max_limit": 8.5},
            {"name": "Dissolved Oxygen", "value": 8.50, "min_limit": 5.0, "max_limit": 14.6},
            {"name": "Turbidity", "value": 6.50, "min_limit": 0.0, "max_limit": 5.0}, # exceeds 5.0 -> qi = 130
            {"name": "Total Dissolved Solids", "value": 300.00, "min_limit": 0.0, "max_limit": 500.0},
        ]
        result = calculate_wqi_for_readings(readings)

        # 1. Multi-model indices
        self.assertIsNotNone(result.get("arithmetic_wqi"))
        self.assertIsNotNone(result.get("geometric_wqi"))
        self.assertIsNotNone(result.get("ccme_wqi"))
        self.assertIn("score", result["ccme_wqi"])
        self.assertIn("tier", result["ccme_wqi"])

        # 2. Aggregation eclipsing detection (Turbidity qi = 130 > 100)
        self.assertTrue(result.get("eclipsing_detected"))
        self.assertIn("Turbidity", result.get("eclipsed_parameters", []))

        # 3. Water use suitability matrix
        matrix = result.get("suitability_matrix", {})
        self.assertIn("potable", matrix)
        self.assertIn("industrial", matrix)
        self.assertIn("irrigation", matrix)
        self.assertIn("aquatic", matrix)
        for cat_name, cat_data in matrix.items():
            self.assertIn("score", cat_data)
            self.assertIn("label", cat_data)
            self.assertIn("tone", cat_data)

        # 4. Remediation priorities
        remediations = result.get("remediation_priorities", [])
        self.assertGreater(len(remediations), 0)
        turb_rem = next((r for r in remediations if r["name"] == "Turbidity"), None)
        self.assertIsNotNone(turb_rem)
        self.assertGreater(turb_rem["wqi_gain"], 0)
        self.assertIn("action", turb_rem)

    def test_scada_wqi_exports_and_view(self):
        """Test SCADA WQI dashboard tab, CSV, JSON, and report export endpoints."""
        plant = TreatmentPlant.objects.create(name="Ruiru Plant", code="RUIRU_PLT")
        user = User.objects.create_superuser(
            username="NCWSC99003",
            email="scada_admin@nairobiwater.co.ke",
            password="TestPassword123!",
        )
        self.client.force_login(user)

        # 1. Tab = wqi HTML view
        resp_tab = self.client.get(f"/qc/scada/?plant={plant.pk}&tab=wqi")
        self.assertEqual(resp_tab.status_code, 200)
        self.assertContains(resp_tab, "Water Quality Index")
        self.assertContains(resp_tab, "chart-wqi-radar-data")

        # 2. CSV Export
        resp_csv = self.client.get(f"/qc/scada/?plant={plant.pk}&export=wqi_csv")
        self.assertEqual(resp_csv.status_code, 200)
        self.assertEqual(resp_csv["Content-Type"], "text/csv")
        self.assertIn("attachment; filename=", resp_csv["Content-Disposition"])

        # 3. JSON Telemetry Export
        resp_json = self.client.get(f"/qc/scada/?plant={plant.pk}&export=wqi_json")
        self.assertEqual(resp_json.status_code, 200)
        data = resp_json.json()
        self.assertEqual(data.get("status"), "success")
        self.assertIn("wqi", data)

        # 4. Official Compliance Dossier
        resp_rep = self.client.get(f"/qc/scada/?plant={plant.pk}&export=wqi_report")
        self.assertEqual(resp_rep.status_code, 200)
        self.assertContains(resp_rep, "Compliance Dossier")


