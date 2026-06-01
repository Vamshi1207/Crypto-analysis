import pandas as pd
import numpy as np


def flatten_and_align_indicators(ind_dict, N):
    aligned = {}

    def pad_list(lst, length, fill=np.nan):
        if lst is None:
            lst = []
        lst = list(lst)
        if len(lst) < length:
            return [fill] * (length - len(lst)) + lst
        return lst[-length:]

    for key, val in ind_dict.items():
        if isinstance(val, list):
            if val and isinstance(val[0], dict):
                subkeys = set()
                for item in val:
                    if isinstance(item, dict):
                        subkeys.update(item.keys())
                for subkey in sorted(subkeys):
                    series = [
                        (item.get(subkey, np.nan) if isinstance(item, dict) else np.nan)
                        for item in val
                    ]
                    aligned[f"{key}_{subkey}"] = pad_list(series, N)
            else:
                aligned[key] = pad_list(val, N)
        else:
            aligned[key] = val

    return aligned


def list_to_rows(df, sequence_mode="latest", max_points=20):
    if df.empty:
        return df.copy()

    out = df.copy()
    list_cols = [col for col in out.columns if out[col].map(lambda x: isinstance(x, list)).any()]

    def normalize_list(v):
        if isinstance(v, list):
            return v
        if pd.isna(v):
            return []
        return [v]

    if sequence_mode == "latest":
        for col in list_cols:
            out[col] = out[col].map(lambda v: (normalize_list(v)[-1] if len(normalize_list(v)) else np.nan))
        return out.reset_index(drop=True)

    raise ValueError("Only 'latest' mode used in production")