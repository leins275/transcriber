# GPU integration test sample

`tests/test_gpu_integration.py` holds the one `@pytest.mark.gpu` test in this
suite: a real end-to-end transcription against the real `large-v3` weights on
a CUDA device. It is opt-in and excluded from the default `pytest` run
(`addopts = -m "not gpu"`), so it never needs a model, a GPU or network
access to pass the default QA gate.

To run it on the reference machine (RTX 4070, real weights available):

1. Drop a short (a few seconds is enough) mono WAV file at
   `tests/data/sample.wav`, **or** point `TRANSCRIBER_TEST_SAMPLE` at one
   anywhere on disk:

   ```
   set TRANSCRIBER_TEST_SAMPLE=D:\path\to\sample.wav
   ```

2. Optionally set `TRANSCRIBER_MODEL_PATH` to an existing local model cache
   directory, and `TRANSCRIBER_TEST_MODEL` to override the model size (the
   default is `large-v3`).

3. Run:

   ```
   uv run pytest -m gpu
   ```

Do **not** commit a sample file to this directory -- keep it local to your
machine. Without a sample or `TRANSCRIBER_TEST_SAMPLE` configured, the test
skips cleanly with an explanatory message; it never downloads a model or
requires CUDA in the default suite.
