def write_chunked_output(df, out_path, token_id, chunk_size=25000):
    total = len(df)

    with out_path.open("w", encoding="utf-8", newline="") as f:
        for i, start in enumerate(range(0, total, chunk_size)):
            chunk = df.iloc[start:start + chunk_size]
            chunk.to_csv(f, index=False, header=(i == 0))