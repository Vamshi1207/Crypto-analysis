from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from xgboost import XGBClassifier


BASE = Path("/app")
if not BASE.exists():
    BASE = Path(__file__).resolve().parent.parent

DATASET_DIR = BASE / "ML_Training_datasets" / "Datasets" / "5"
MODEL_DIR = BASE / "ML_Training_datasets" / "Models" / "5_baseline_h10_long_only"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

RANDOM_STATE = 42
TARGET_HORIZON = 10
TARGET_COL = f"future_return_pct_{TARGET_HORIZON}"
EXPECTED_HORIZON_SECONDS = TARGET_HORIZON * 5
UP_PROBABILITY_THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]

BASE_FEATURE_COLS = [
    "rsi",
    "ema10",
    "ema20",
    "ema50",
    "ema100",
    "ema200",
    "ema_cross",
    "macd_line",
    "macd_signal",
    "macd_hist",
    "vwap",
    "adx",
    "plus_di",
    "minus_di",
    "stoch_k",
    "stoch_d",
    "boll_upper",
    "boll_middle",
    "boll_lower",
    "boll_percent",
    "atr",
    "obv",
    "supertrend_value",
    "cci",
    "roc",
    "momentum3",
    "volume_sum",
    "volume_avg",
    "range_pct",
    "open_rel",
    "high_rel",
    "low_rel",
    "close_rel",
    "hour",
    "minute",
    "weekday",
    "current_close",
    "supertrend_trend_enc",
    "rsi_lag_1",
    "rsi_roll_3",
    "rsi_roll_5",
    "atr_roll_3",
    "atr_roll_5",
    "volume_avg_roll_3",
    "volume_avg_roll_5",
    "obv_roll_3",
]

OPTIONAL_FEATURE_COLS = [
    "last_5m_buyCount",
    "last_5m_sellCount",
    "last_5m_buyVolumeSol",
    "last_5m_sellVolumeSol",
    "last_5m_priceSol",
]


def natural_dataset_files(dataset_dir: Path) -> list[Path]:
    return sorted(
        dataset_dir.glob("memecoin_training_dataset_*.csv"),
        key=lambda path: int(path.stem.rsplit("_", 1)[-1]),
    )


def load_dataset() -> pd.DataFrame:
    csv_files = natural_dataset_files(DATASET_DIR)
    if not csv_files:
        raise ValueError(f"No dataset CSVs found in {DATASET_DIR}")

    frames = []
    for path in csv_files:
        df_part = pd.read_csv(path)
        df_part["source_file"] = path.name
        if "token_id" not in df_part.columns:
            df_part["token_id"] = path.stem.replace("memecoin_training_dataset_", "token_")
        if "token_name" not in df_part.columns:
            df_part["token_name"] = df_part["token_id"]
        frames.append(df_part)

    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.sort_values(["token_id", "timestamp"]).reset_index(drop=True)


def add_features(df: pd.DataFrame) -> tuple[pd.DataFrame, LabelEncoder]:
    le_supertrend = LabelEncoder()
    df["supertrend_trend_enc"] = le_supertrend.fit_transform(df["supertrend_trend"].astype(str))

    def add_group_features(token_id: str, group: pd.DataFrame) -> pd.DataFrame:
        group = group.copy()
        group["token_id"] = token_id
        group["rsi_lag_1"] = group["rsi"].shift(1)
        group["rsi_roll_3"] = group["rsi"].rolling(3, min_periods=1).mean()
        group["rsi_roll_5"] = group["rsi"].rolling(5, min_periods=1).mean()
        group["atr_roll_3"] = group["atr"].rolling(3, min_periods=1).mean()
        group["atr_roll_5"] = group["atr"].rolling(5, min_periods=1).mean()
        group["volume_avg_roll_3"] = group["volume_avg"].rolling(3, min_periods=1).mean()
        group["volume_avg_roll_5"] = group["volume_avg"].rolling(5, min_periods=1).mean()
        group["obv_roll_3"] = group["obv"].rolling(3, min_periods=1).mean()
        group["label_elapsed_seconds"] = (
            group["timestamp"].shift(-TARGET_HORIZON) - group["timestamp"]
        ).dt.total_seconds()
        return group

    df = pd.concat(
        [add_group_features(token_id, group) for token_id, group in df.groupby("token_id", sort=False)],
        ignore_index=True,
    )
    df = df.replace([np.inf, -np.inf], np.nan)
    return df.dropna().reset_index(drop=True), le_supertrend


def direction_label(series: pd.Series) -> pd.Series:
    return pd.Series(np.where(series > 0.0, "up", "down_or_flat"), index=series.index)


def long_only_threshold_metrics(
    returns: np.ndarray,
    y_true_enc: np.ndarray,
    up_prob: np.ndarray,
    up_class_index: int,
    thresholds: list[float],
) -> pd.DataFrame:
    rows = []
    true_up = y_true_enc == up_class_index

    for threshold in thresholds:
        selected = up_prob >= threshold
        selected_count = int(selected.sum())
        coverage = float(selected.mean())

        if selected_count == 0:
            rows.append(
                {
                    "threshold": threshold,
                    "selected_rows": 0,
                    "coverage": coverage,
                    "win_rate": np.nan,
                    "avg_return_pct": np.nan,
                    "median_return_pct": np.nan,
                    "precision_up": np.nan,
                }
            )
            continue

        selected_returns = returns[selected]
        rows.append(
            {
                "threshold": threshold,
                "selected_rows": selected_count,
                "coverage": coverage,
                "win_rate": float((selected_returns > 0).mean()),
                "avg_return_pct": float(selected_returns.mean()),
                "median_return_pct": float(np.median(selected_returns)),
                "precision_up": float(true_up[selected].mean()),
            }
        )

    return pd.DataFrame(rows)


def main() -> None:
    print(f"Loading dataset from {DATASET_DIR}")
    df = load_dataset()
    raw_rows = len(df)
    raw_tokens = df["token_id"].nunique()

    df, le_supertrend = add_features(df)

    missing_required = [col for col in BASE_FEATURE_COLS + [TARGET_COL] if col not in df.columns]
    if missing_required:
        raise ValueError(f"Missing required columns: {missing_required}")

    feature_cols = BASE_FEATURE_COLS + [col for col in OPTIONAL_FEATURE_COLS if col in df.columns]
    tokens = np.array(sorted(df["token_id"].unique()))

    train_tokens, test_tokens = train_test_split(tokens, test_size=0.20, random_state=RANDOM_STATE)
    train_tokens, val_tokens = train_test_split(train_tokens, test_size=0.20, random_state=RANDOM_STATE)

    train_mask = df["token_id"].isin(train_tokens)
    val_mask = df["token_id"].isin(val_tokens)
    test_mask = df["token_id"].isin(test_tokens)

    X_train = df.loc[train_mask, feature_cols]
    X_val = df.loc[val_mask, feature_cols]
    X_test = df.loc[test_mask, feature_cols]

    y_train = direction_label(df.loc[train_mask, TARGET_COL])
    y_val = direction_label(df.loc[val_mask, TARGET_COL])
    y_test = direction_label(df.loc[test_mask, TARGET_COL])

    dir_encoder = LabelEncoder()
    y_train_enc = dir_encoder.fit_transform(y_train)
    y_val_enc = dir_encoder.transform(y_val)
    y_test_enc = dir_encoder.transform(y_test)

    class_counts = y_train.value_counts().sort_index()
    class_weight_scale = len(y_train) / (len(class_counts) * class_counts)
    sample_weight = y_train.map(class_weight_scale).to_numpy()

    model = XGBClassifier(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.85,
        colsample_bytree=0.85,
        min_child_weight=10,
        reg_lambda=2.0,
        random_state=RANDOM_STATE,
        objective="binary:logistic",
        eval_metric="logloss",
        tree_method="hist",
        n_jobs=-1,
    )

    print(
        json.dumps(
            {
                "raw_rows": raw_rows,
                "rows_after_features": len(df),
                "tokens": raw_tokens,
                "features": len(feature_cols),
                "train_rows": int(train_mask.sum()),
                "val_rows": int(val_mask.sum()),
                "test_rows": int(test_mask.sum()),
                "train_tokens": len(train_tokens),
                "val_tokens": len(val_tokens),
                "test_tokens": len(test_tokens),
            },
            indent=2,
        )
    )

    model.fit(
        X_train,
        y_train_enc,
        sample_weight=sample_weight,
        eval_set=[(X_val, y_val_enc)],
        verbose=False,
    )

    val_pred = model.predict(X_val)
    test_pred = model.predict(X_test)
    test_proba = model.predict_proba(X_test)
    up_class_index = int(np.where(dir_encoder.classes_ == "up")[0][0])
    test_up_prob = test_proba[:, up_class_index]
    test_returns = df.loc[test_mask, TARGET_COL].to_numpy()

    threshold_metrics = long_only_threshold_metrics(
        returns=test_returns,
        y_true_enc=y_test_enc,
        up_prob=test_up_prob,
        up_class_index=up_class_index,
        thresholds=UP_PROBABILITY_THRESHOLDS,
    )

    split_summary = pd.DataFrame(
        [
            {"split": "train", "tokens": len(train_tokens), "rows": int(train_mask.sum())},
            {"split": "validation", "tokens": len(val_tokens), "rows": int(val_mask.sum())},
            {"split": "test", "tokens": len(test_tokens), "rows": int(test_mask.sum())},
        ]
    )

    label_elapsed = df.loc[test_mask, "label_elapsed_seconds"]
    metrics = {
        "target_col": TARGET_COL,
        "expected_horizon_seconds": EXPECTED_HORIZON_SECONDS,
        "raw_rows": raw_rows,
        "rows_after_features": len(df),
        "features": len(feature_cols),
        "tokens": raw_tokens,
        "validation_accuracy": float(accuracy_score(y_val_enc, val_pred)),
        "test_accuracy": float(accuracy_score(y_test_enc, test_pred)),
        "test_always_long_win_rate": float((test_returns > 0).mean()),
        "test_always_long_avg_return_pct": float(test_returns.mean()),
        "test_label_elapsed_seconds_median": float(label_elapsed.median()),
        "test_label_elapsed_seconds_p90": float(label_elapsed.quantile(0.90)),
        "test_label_elapsed_seconds_p99": float(label_elapsed.quantile(0.99)),
    }

    print("\nClassification report:")
    print(classification_report(y_test_enc, test_pred, target_names=dir_encoder.classes_))
    print("Confusion matrix:")
    print(confusion_matrix(y_test_enc, test_pred))
    print("\nLong-only threshold metrics:")
    print(threshold_metrics.to_string(index=False))
    print("\nSummary:")
    print(json.dumps(metrics, indent=2))

    feature_importance = pd.Series(model.feature_importances_, index=feature_cols).sort_values(ascending=False)

    joblib.dump(model, MODEL_DIR / "xgb_baseline_h10_long_only.joblib")
    joblib.dump(
        {
            "feature_cols": feature_cols,
            "target_col": TARGET_COL,
            "direction_classes": list(dir_encoder.classes_),
            "supertrend_classes": list(le_supertrend.classes_),
            "train_tokens": list(train_tokens),
            "val_tokens": list(val_tokens),
            "test_tokens": list(test_tokens),
            "thresholds": UP_PROBABILITY_THRESHOLDS,
            "metrics": metrics,
        },
        MODEL_DIR / "baseline_h10_artifacts.joblib",
    )

    split_summary.to_csv(MODEL_DIR / "split_summary.csv", index=False)
    threshold_metrics.to_csv(MODEL_DIR / "test_threshold_metrics.csv", index=False)
    feature_importance.to_csv(MODEL_DIR / "feature_importance.csv", header=["importance"])
    with (MODEL_DIR / "metrics_summary.json").open("w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    print(f"\nSaved artifacts to {MODEL_DIR}")


if __name__ == "__main__":
    main()
