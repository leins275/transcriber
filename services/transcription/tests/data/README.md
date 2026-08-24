# GPU integration test samples

`tests/test_gpu_integration.py` holds the `@pytest.mark.gpu` tests in this
suite: real end-to-end transcriptions against the real `large-v3` weights on
a CUDA device. They are opt-in and excluded from the default `pytest` run
(`addopts = -m "not gpu"`), so they never need a model, a GPU or network
access to pass the default QA gate.

Two kinds of sample are used:

| Sample | Env var | Default path | Used by |
|---|---|---|---|
| Any short speech | `TRANSCRIBER_TEST_SAMPLE` | `tests/data/sample.wav` | the end-to-end / model-caching test |
| English speech | `TRANSCRIBER_TEST_SAMPLE_EN` | `tests/data/sample-en.wav` | constrained language detection (F2 FR-1, FR-4) |
| Russian speech | `TRANSCRIBER_TEST_SAMPLE_RU` | `tests/data/sample-ru.wav` | constrained language detection (F2 FR-1, FR-4) |

The language samples must be speech you know the language of: the test
asserts that a sample with no requested `language` comes back decoded in the
language actually spoken (`transcript.json.language`, the ledger row, and the
script the text is written in). A few sentences -- 15-30 s -- is plenty. Any
container faster-whisper can decode works (wav, m4a, webm, mp4).

To run them on the reference machine (RTX 4070, real weights available):

1. Drop a short (a few seconds is enough) mono WAV file at
   `tests/data/sample.wav`, **or** point `TRANSCRIBER_TEST_SAMPLE` at one
   anywhere on disk:

   ```
   set TRANSCRIBER_TEST_SAMPLE=D:\path\to\sample.wav
   set TRANSCRIBER_TEST_SAMPLE_EN=D:\path\to\english-speech.wav
   set TRANSCRIBER_TEST_SAMPLE_RU=D:\path\to\russian-speech.wav
   ```

2. Optionally set `TRANSCRIBER_MODEL_PATH` to an existing local model cache
   directory, and `TRANSCRIBER_TEST_MODEL` to override the model size (the
   default is `large-v3`). `TRANSCRIBER_MODEL_PATH` is the *snapshot*
   directory itself (`...\models\faster-whisper-large-v3`), not its parent.

   A dev checkout's `.venv` has no `nvidia-*` wheels unless it was synced
   with `--extra cuda`, and these tests construct `JobManager` directly, so
   nothing calls `runtime_dlls.register_cuda_dll_dirs` for them. Either sync
   the extra, or prepend an existing CUDA runtime's `nvidia\*\bin`
   directories (e.g. the installed app's
   `%LOCALAPPDATA%\Transcriber\runtime\nvidia\*\bin`) onto `PATH` for the
   run -- otherwise CTranslate2 cannot load cuBLAS/cuDNN and the CUDA device
   fails.

3. Run:

   ```
   uv run pytest -m gpu
   ```

Do **not** commit a sample file to this directory -- keep it local to your
machine (nothing here is git-ignored -- keep samples outside the repo and
point the env vars at them, which is why they exist). Without the matching
sample configured, each test skips cleanly with an explanatory message naming
the env var it wanted; nothing here ever downloads a model or requires CUDA
in the default suite.
