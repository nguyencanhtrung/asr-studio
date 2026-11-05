# ASR measurement for v0.2.1

v0.2.1 is the first release with four selectable ASR models. This is a short measurement of quality, speed and memory for each one. It is a quick reference, not a full benchmark.

## Setup

| Field | Value |
| --- | --- |
| Commit | `188f172293bd122ab4ed91b5630d251e4749feef` |
| Date | 2026-11-05 |
| GPU | NVIDIA GeForce RTX 5080, 16 GB, driver 590.48.01 |
| CPU | AMD Ryzen 9 9950X, 16 cores, 60 GB RAM |
| Path | The real product: a built image, Compose, one real task per cell |

The clips hold the first utterances of each set in file order, in one 16 kHz mono file with 0.5 s of silence between them. GPU clips last about 3 minutes and CPU clips about 1 minute. CPU and GPU clips hold different utterances, so do not compare their WER.

WER and CER use Unicode NFC, lowercase and no punctuation. Numerals and spelling variants are not unified, so some errors are only orthographic.

| Set | Source | Revision | Language | Licence |
| --- | --- | --- | --- | --- |
| Vietnamese, read sentences | [VIVOS test](https://huggingface.co/datasets/ademax/vivos-vie-speech2text), community copy | `1c937fd` | Vietnamese | Original VIVOS: CC BY-NC-SA 4.0 |
| Vietnamese, conversation | [VietMed test](https://huggingface.co/datasets/leduckhai/VietMed) | `f302146` | Vietnamese, medical | MIT |
| Mixed | [ViMedCSS test](https://huggingface.co/datasets/tensorxt/ViMedCSS), community copy | `b6959a1` | Vietnamese with English terms, medical | CC BY 4.0 |
| English | [LibriSpeech dummy](https://huggingface.co/datasets/hf-internal-testing/librispeech_asr_dummy) | `5be9148` | English | Dataset's own |

## Results on GPU

Lower is better. Whisper models use `float16` and Qwen3-ASR uses `bfloat16`.

| WER | Whisper large-v3 | Whisper small | PhoWhisper-large | Qwen3-ASR-1.7B |
| --- | --- | --- | --- | --- |
| Vietnamese, read sentences | 0.190 | 0.251 | **0.035** | 0.050 |
| Vietnamese, conversation | 0.789 (see below) | 0.201 | **0.056** | 0.059 |
| Mixed | 0.294 | 0.241 | 0.364 | **0.150** |
| English | 0.057 | 0.071 | 0.167 | **0.041** |

| Other measures | Whisper large-v3 | Whisper small | PhoWhisper-large | Qwen3-ASR-1.7B |
| --- | --- | --- | --- | --- |
| English terms found, mixed set (of 24) | 6 | 4 | 6 | **12** |
| Transcription stage RTF | 0.055 to 0.126 | 0.035 to 0.047 | 0.062 to 0.080 | 0.016 to 0.022 |
| Whole task RTF, with diarization | 0.097 to 0.169 | 0.078 to 0.091 | 0.102 to 0.127 | 0.073 to 0.080 |
| Peak GPU memory for the transcription stage, MiB | 4306 to 5522 | 1010 to 1106 | 4274 | 5320 to 5852 |

RTF is processing time divided by audio length. Whole task RTF includes one model load. Diarization alone peaks at about 4228 MiB, so Whisper small's whole task reaches that.

## Results on CPU

`int8`. Qwen3-ASR-1.7B is refused by design with `model_requires_gpu`.

| WER | Whisper large-v3 | Whisper small | PhoWhisper-large |
| --- | --- | --- | --- |
| Vietnamese, read sentences | 0.087 | 0.167 | **0.007** |
| Vietnamese, conversation | 0.495 | 0.160 | **0.080** |
| Mixed | 0.485 | **0.234** | 0.491 |
| English | 0.060 | **0.036** | 0.120 |

| Other measures | Whisper large-v3 | Whisper small | PhoWhisper-large |
| --- | --- | --- | --- |
| Whole task RTF | 0.557 to 1.472 | 0.374 to 0.426 | 0.584 to 0.703 |
| Peak container memory, MiB | 2824 to 4784 | 1767 to 2374 | 2538 to 2842 |

CPU clips hold 34 to 57 s of audio and 83 to 212 reference words. One word changes the read sentences result by 0.007.

## Whisper large-v3 on Vietnamese

Whisper large-v3 scored 0.789 on the conversation clip, with 484 of 692 words deleted and a closing line about subscribing to a channel repeated. The clip is stitched, so a control ran the same 27 utterances in other forms on GPU:

| Form | Whisper large-v3 | Whisper small |
| --- | --- | --- |
| Concatenated, 0.5 s gaps | 0.789 | 0.201 |
| Concatenated in reverse, 0.5 s gaps | 0.152 | 0.324 |
| Concatenated, 2 s gaps | 0.970 | 0.244 |
| Each utterance as its own task, pooled | 0.303 | 0.189 |
| Utterances returning the channel line when alone | **11 of 27** | 0 of 27 |

The result depends on the stitching and is also present without it. **The cause is not known.** This product path runs Whisper with `language=None` and `multilingual=True`, so the language hint does not reach the decoder. These settings match v0.2.0, so this is not a new regression. See [#168](https://github.com/nguyencanhtrung/asr-studio/issues/168).

## Limits

- One clip per set and one measured run per cell. Differences of a few points are not meaningful, and there are no confidence intervals.
- Whisper large-v3 changed between two runs of the same English clip. Other cells were not repeated.
- The utterances come from the start of each file and are not a random sample.
- VIVOS and ViMedCSS are community copies, not checked against the original releases.
- The mixed and conversation sets are medical. They may not represent business or technical meetings.
- Stitched utterances are not a real meeting. Real meetings have no reference transcript here.
- Do not compare these numbers with public leaderboards. The subset, normalization and path differ.
