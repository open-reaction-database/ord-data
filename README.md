# ord-data

![](https://github.com/Open-Reaction-Database/ord-data/workflows/Validation/badge.svg)
[![DOI](https://zenodo.org/badge/283813042.svg)](https://zenodo.org/badge/latestdoi/283813042)

## Getting the Data

The datasets live under [`data/`](data) and are stored with
[Git LFS](https://git-lfs.com/). LFS reads are redirected to the
[Hugging Face mirror](https://huggingface.co/datasets/open-reaction-database/ord-data)
via [`.lfsconfig`](.lfsconfig), so dataset objects are fetched from Hugging
Face's CDN rather than from GitHub's shared (and limited) LFS bandwidth. This is
automatic — you do not need to configure anything.

### Option 1: Clone the repository

```bash
git clone https://github.com/open-reaction-database/ord-data.git
```

With [Git LFS](https://git-lfs.com/) installed, this pulls every dataset object
from the Hugging Face mirror and gives you the full Git history with the data in
place.

### Option 2: Download only the data (a subset, or without Git history)

```bash
pip install huggingface_hub
python scripts/download_from_huggingface.py
```

(If you use [uv](https://docs.astral.sh/uv/), `uv run scripts/download_from_huggingface.py`
installs the dependencies for you.)

The script mirrors the `data/` directory from the Hugging Face dataset into your
local checkout. Pass `--allow-pattern 'data/4d/*.parquet'` (repeatable) to download
only a subset, or `--output-dir <path>` to write somewhere other than the
repository root. To skip LFS entirely during the clone and fetch the data
afterward:

```bash
GIT_LFS_SKIP_SMUDGE=1 git clone https://github.com/open-reaction-database/ord-data.git
cd ord-data
python scripts/download_from_huggingface.py
```

You can also browse and download datasets directly from the
[Hugging Face dataset page](https://huggingface.co/datasets/open-reaction-database/ord-data).

For how this LFS / Hugging Face mirror setup works (and what it means for
contributors), see
[Git LFS and the Hugging Face mirror](#git-lfs-and-the-hugging-face-mirror)
below.

## Data Manipulation

The `ord-data` repository contains the Open Reaction Database (ORD) as Parquet files stored in the
[`data`](data) directory. Each file holds one dataset: the reactions are serialized Protobuf
messages (one per row) and the dataset name/description/ID travel in the file metadata, so a
dataset round-trips losslessly through `ord_schema`. The user can convert the data into human
readable text format, *.pbtxt.

```python
# import requirements
from ord_schema.datasets import load_dataset
from ord_schema.message_helpers import save_message

# load the parquet file as a Dataset proto
dataset = load_dataset("input_fname.parquet")
# save the ord file as human readable text
save_message(dataset, "output_fname.pbtxt")
```

`load_dataset` deserializes every reaction into memory. For the larger datasets —
notably the USPTO grants file, with 1.7M reactions — read them a piece at a time
instead with `ord_schema.parquet.DatasetView`, which quacks like a `Dataset` for
`name`/`description`/`dataset_id` while its `reactions` stream off disk. Each
`Reaction` carries its own `reaction_id`, so a view is all most readers need; the
same module's `load_reaction` and `iter_reactions` are there to select by ID or
to read a single row group, the unit of parallelism for fanning out over a large
file.

We can also convert ORD data into JSON format.

```python
# import requirements
import json

from ord_schema.parquet import DatasetView
from google.protobuf.json_format import MessageToJson

input_fname = "sample_file.parquet"
dataset = DatasetView(input_fname)

# take one reaction message from the dataset for example
rxn = next(iter(dataset.reactions))
rxn_json = json.loads(
    MessageToJson(
        message=rxn,
        always_print_fields_with_no_presence=False,
        preserving_proto_field_name=True,
        indent=2,
        sort_keys=False,
        use_integers_for_enums=False,
        descriptor_pool=None,
        float_precision=None,
        ensure_ascii=True,
    )
)

print(
    f"We have converted the {input_fname} to JSON format shown as below, \n{rxn_json}"
)
```

## Git LFS and the Hugging Face mirror

Dataset files under [`data/`](data) are stored with Git LFS. Clone and fork
traffic was dominating GitHub's shared LFS bandwidth quota, so the repository is
configured to keep that traffic off GitHub while leaving GitHub authoritative
for the data:

- **Reads come from Hugging Face.** [`.lfsconfig`](.lfsconfig) points `lfs.url`
  at the
  [Hugging Face mirror](https://huggingface.co/datasets/open-reaction-database/ord-data),
  so clones and forks fetch LFS objects from HF's CDN instead of GitHub.
- **GitHub remains the source of truth.** LFS objects are always written to
  GitHub (storage there is fine; only download bandwidth was the problem), and
  the [mirror workflow](.github/workflows/huggingface_mirror.yml) copies them to
  Hugging Face after every merge to `main`. Hugging Face is purely a read
  replica — every object is always retrievable from GitHub.
- **LFS is scoped to `data/`** (see [`.gitattributes`](.gitattributes)). A new
  dataset staged at the repository root is an ordinary Git file, so submissions
  can be pushed from a fork with no LFS configuration; the submission workflow
  turns the file into an LFS object when it moves it into `data/`.

### For contributors

- **Submitting a new dataset:** nothing special is required — stage your file at
  the repository root and open a PR (see [CONTRIBUTING.md](CONTRIBUTING.md) and
  the
  [Submission Workflow](https://docs.open-reaction-database.org/en/latest/submissions.html)).
- **Editing a file that already lives under `data/` from a fork:** that file is
  an LFS object, so point LFS uploads at your own fork once before pushing (you
  cannot write to the canonical repository's LFS store):

  ```bash
  git config lfs.pushurl https://github.com/<your-username>/ord-data.git/info/lfs
  ```

### For maintainers (CI)

Freshly pushed objects are not on the Hugging Face mirror until the post-merge
mirror job runs, so CI and the mirror override the read endpoint back to GitHub
at runtime (`git config lfs.url …`):

- [`validation.yml`](.github/workflows/validation.yml) pulls only each matrix
  shard's objects from GitHub, sparsely, instead of the whole dataset in every
  job.
- [`submission.yml`](.github/workflows/submission.yml) reads from GitHub so fork
  and branch submissions are validated before their bytes reach Hugging Face.
- [`huggingface_mirror.yml`](.github/workflows/huggingface_mirror.yml) reads the
  to-be-mirrored objects from GitHub.

## Contributing

Please see the [Submission Workflow](https://docs.open-reaction-database.org/en/latest/submissions.html) documentation. Make sure to review the [license](https://github.com/open-reaction-database/ord-data/blob/main/LICENSE) and [terms of use](https://github.com/open-reaction-database/ord-data/blob/main/CONTRIBUTING.md#terms-of-use).

## Maintainer notes

### Skipping the `Update submission` step

The submission workflow's `Update submission` step runs
[`scripts/process_dataset.py`](scripts/process_dataset.py) `--update --cleanup`
to assign reaction/dataset IDs and timestamps to newly
submitted files and rewrite them to the canonical on-disk format. For
maintainer PRs that touch dataset files but should *not* be re-processed
this way — e.g., format conversions or mass migrations of already-finalized
data — apply the `skip-update-submission` label to the PR. The validation
side of the workflow still runs.

### Converting datasets to Parquet

Published datasets are stored as Parquet only, and new submissions are written
that way by `process_dataset.py`, so `data/` holds no `.pb.gz` to convert.
[`scripts/convert_to_parquet.py`](scripts/convert_to_parquet.py) remains for the
case where one lands there anyway: it globs every `data/**/*.pb.gz`, converts
each 1:1 (carrying the existing `dataset_id`), and skips any output that already
exists — so it is safe to re-run and writes only what is missing. Converting the
corpus is this script's job, not a side effect of editing one dataset, which is
why `process_dataset.py` leaves an edited file in whatever format it has.

It needs `ord_schema`, which comes from the `pipeline` dependency group. Because
it reads `.pb.gz` content, pull the inputs first:

```bash
uv sync --only-group pipeline

git lfs pull --include="data/**/*.pb.gz"  # the converter reads pb.gz content
uv run --no-sync python scripts/convert_to_parquet.py --dry-run  # preview
uv run --no-sync python scripts/convert_to_parquet.py  # write the Parquet versions
git rm data/*/*.pb.gz  # keep only the Parquet versions
```

Commit the new `.parquet` files (they become LFS objects), push them (see
[Pushing new LFS objects](#pushing-new-lfs-objects)), and open the PR with the
`skip-update-submission` label. Validation runs against the full dataset on
merge to `main`.

### Pushing new LFS objects

[`.lfsconfig`](.lfsconfig) routes LFS **reads** to the Hugging Face mirror and
deliberately sets no `pushurl`, so a plain push would try to upload new objects
to HF — which you cannot write. Point LFS uploads at GitHub for the push, and
make sure git can authenticate to `github.com` over **HTTPS** for the LFS API
(the LFS endpoint is HTTPS even when your `git` remote is SSH). The simplest
auth is the GitHub CLI:

```bash
git config lfs.pushurl https://github.com/open-reaction-database/ord-data.git/info/lfs
gh auth setup-git  # let git use your gh token for github.com over HTTPS
git push -u origin <branch>
```

Or, as a one-off without persisting any config:

```bash
git -c lfs.pushurl=https://github.com/open-reaction-database/ord-data.git/info/lfs \
    -c 'credential.https://github.com.helper=!gh auth git-credential' \
    push -u origin <branch>
```

Reads stay on the mirror; only your uploads go to GitHub. On merge to `main`,
`huggingface_mirror.yml` copies the new objects to Hugging Face.

## License

This repository carries two licenses, because it holds both data and code:

| what | license | file |
| --- | --- | --- |
| The datasets under `data/` (and the repository metadata describing them) | [CC-BY-SA-4.0](LICENSE) | `LICENSE` |
| The code under `scripts/` and `.github/` | [Apache-2.0](LICENSE-CODE) | `LICENSE-CODE` |

If you are using ORD data, CC-BY-SA-4.0 is the license that applies to you; it
is what `CITATION.cff` and the [Hugging Face
mirror](https://huggingface.co/datasets/open-reaction-database/ord-data) declare.

The code carries a separate license because Creative Commons licenses are not
intended for software — [Creative Commons recommends against
it](https://creativecommons.org/faq/#can-i-apply-a-creative-commons-license-to-software)
— and because the project's other code repositories, including
[ord-schema](https://github.com/Open-Reaction-Database/ord-schema), are
Apache-2.0.
