"""AGRIVOS AI module — waiting-time & congestion prediction.

Per the SIH deck: "AI/ML: Python Scikit-learn — waiting-time / congestion
prediction".

Two models, both RandomForest, trained on synthetic historical records
generated from realistic centre behaviour (queue length, slot hour, day of
week, centre capacity, service rate, arrivals ... -> actual wait in minutes):

1. wait_model     : predicts minutes of waiting for a farmer given queue
                   position, slot hour, day, centre load & service rate.
2. congestion_model: predicts congestion level (0=calm .. 3=severe) for a
                   centre/slot/hour so the UI can badge busy slots.

Training happens on first import (~1s) — no external dataset required.
"""
from datetime import datetime
import random

import numpy as np
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

RNG = random.Random(42)
N_TRAIN = 4000

# feature vector:
# [queue_ahead, hour, day_of_week, capacity, avg_service_min, arrivals_last_hour]
# targets: wait minutes / congestion class
FEATURES = ["queue_ahead", "hour", "day_of_week", "capacity", "service_min", "arrivals"]

def _synthetic_history():
    X, y_wait, y_cong = [], [], []
    for _ in range(N_TRAIN):
        capacity = RNG.choice([8, 10, 12, 15, 20])
        service = RNG.uniform(4, 12)               # minutes per farmer
        queue = RNG.randint(0, 40)
        hour = RNG.randint(8, 17)
        day = RNG.randint(0, 6)
        arrivals = max(0, int(np.random.normal(0.8 * capacity, 3)))
        # market-day effect: Sat (5) busier
        day_factor = 1.25 if day == 5 else 1.0
        # rush hours: opening & post-lunch
        hour_factor = 1.35 if hour in (9, 14) else (0.85 if hour in (11, 16) else 1.0)
        wait = queue * service * day_factor * hour_factor * RNG.uniform(0.85, 1.15)
        wait = max(0, wait + RNG.gauss(0, 3))
        load_ratio = (queue + arrivals) / max(capacity, 1)
        congestion = 0 if load_ratio < 0.6 else 1 if load_ratio < 1.2 else 2 if load_ratio < 2.0 else 3
        X.append([queue, hour, day, capacity, service, arrivals])
        y_wait.append(wait)
        y_cong.append(congestion)
    return np.array(X, dtype=float), np.array(y_wait), np.array(y_cong)


class CongestionPredictor:
    """RandomForest models for wait-time (min) and congestion class."""

    LABELS = ["Calm", "Moderate", "Busy", "Severe"]

    def __init__(self):
        X, y_wait, y_cong = _synthetic_history()
        self.wait_model = RandomForestRegressor(n_estimators=120, max_depth=14,
                                                random_state=42, n_jobs=-1)
        self.wait_model.fit(X, y_wait)
        self.cong_model = RandomForestClassifier(n_estimators=120, max_depth=14,
                                                 random_state=42, n_jobs=-1)
        self.cong_model.fit(X, y_cong)

    # -------------------------------------------------- public API
    def predict_wait_minutes(self, queue_ahead: int, when: datetime,
                             capacity: int, service_min: float,
                             arrivals_last_hour: int) -> float:
        x = [[queue_ahead, when.hour, when.weekday(), capacity,
              service_min, arrivals_last_hour]]
        return float(max(0.0, round(self.wait_model.predict(x)[0], 1)))

    def predict_congestion(self, when: datetime, capacity: int,
                           arrivals_last_hour: int) -> int:
        x = [[0, when.hour, when.weekday(), capacity, 7.0, arrivals_last_hour]]
        return int(self.cong_model.predict(x)[0])

    def congestion_label(self, level: int) -> str:
        return self.LABELS[min(level, 3)]


# module-level singleton — trained once at startup
PREDICTOR = CongestionPredictor()

if __name__ == "__main__":
    now = datetime.now()
    print("wait (queue 10, 9am):", PREDICTOR.predict_wait_minutes(10, now, 12, 7.5, 10), "min")
    print("congestion:", PREDICTOR.congestion_label(PREDICTOR.predict_congestion(now, 12, 25)))
