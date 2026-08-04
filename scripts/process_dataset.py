# Copyright 2020 Open Reaction Database Project Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Processes the dataset files added or changed by a submission pull request.

Driven by .github/workflows/submission.yml; contributors do not run it. Inputs come
from a git diff against --base, so it is written against this repository's layout and
branch conventions.

By default the script only validates the input Dataset messages. Validation may
introduce changes to the Reaction messages, such as the addition of SMILES for compounds
identified only by NAME.

With --update, the script also performs database-specific updates (such as adding
record IDs) and writes each dataset to its canonical path under data/. With --cleanup,
it records those input to output moves in git's index. Contributors preparing a dataset
should validate with ord_schema.scripts.validate_dataset instead.

A new submission is written as Parquet, which supports random access by row group and
streaming iteration without holding a whole dataset in memory. Submissions may still
arrive in any accepted format -- the serialized-proto suffixes are convenient to
produce -- but that is what lands under data/. Editing a dataset already in data/
leaves it in the format it has, so correcting one dataset never renames a published
path or drops its .pb.gz. Passing --output_format overrides both, which is how a
deliberate one-off conversion is done.
"""

import argparse
import dataclasses
import glob
import os
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping

import github
from ord_schema import (
    atomic_io,
    message_helpers,
    parquet,
    updates,
    validations,
)
from ord_schema.datasets import save_dataset
from ord_schema.logging import get_logger, silence_rdkit_logs
from ord_schema.proto import dataset_pb2

logger = get_logger(__name__)

# Parquet is the standard on-disk format; see the module docstring.
DEFAULT_OUTPUT_FORMAT = ".parquet"


@dataclasses.dataclass(eq=True, frozen=True, order=True)
class FileStatus:
    """A filename and its status in Git."""

    filename: str
    status: str
    original_filename: str

    def __post_init__(self) -> None:
        """Validates that the Git status is one of {'A', 'D', 'M', 'R'}."""
        if self.status[0] not in ["A", "D", "M", "R"]:
            raise ValueError(f"unsupported file status: {self.status}")


def _get_inputs(args: argparse.Namespace) -> list[FileStatus]:
    """Gets a list of Dataset proto filenames to process.

    Returns:
        List of FileStatus objects.

    Raises:
        ValueError: If a git-diff status is not one of {'A', 'D', 'M', 'R'}.
    """
    if args.input_pattern:
        # Setting recursive=True allows recursive matching with '**'.
        filenames = glob.glob(args.input_pattern, recursive=True)
        return [FileStatus(filename, "A", "") for filename in filenames]
    if args.input_file:
        inputs = []
        with pathlib.Path(args.input_file).open() as f:
            for line in f:
                fields = line.strip().split("\t")
                if len(fields) == 3:
                    status, original_filename, filename = fields
                    if not status.startswith("R"):
                        raise ValueError(f"malformed status line: {line.strip()}")
                else:
                    status, filename = fields
                    if not status.startswith(("A", "D", "M")):
                        raise ValueError(f"unsupported git-diff status: {status}")
                    original_filename = ""
                inputs.append(FileStatus(filename, status, original_filename))
        return inputs
    raise ValueError("one of --input_pattern or --input_file is required")


def cleanup(filename: str, output_filename: str) -> None:
    """Reflects the (input → output) submission move in git's index.

    If ``output_filename`` does not exist yet (the in-memory write path runs
    cleanup before writing), do ``git mv`` so git records the rename and the
    subsequent write overwrites the destination. If ``output_filename``
    already exists (the streaming path publishes via atomic ``os.replace``
    first), the move is already on disk; ``git rm`` removes the input from
    git's index and ``git diff -M`` detects the rename via content similarity.

    Args:
        filename: Original dataset filename.
        output_filename: Updated dataset filename.
    """
    if filename == output_filename:
        logger.info("editing an existing dataset; no cleanup needed")
        return  # Reuse the existing dataset ID.
    if pathlib.Path(output_filename).exists():
        args = ["git", "rm", "-f", filename]
    else:
        args = ["git", "mv", filename, output_filename]
    logger.info("Running command: %s", " ".join(args))
    # (internal command, no untrusted input)
    subprocess.run(args, check=True)


def _get_reaction_ids(
    dataset: dataset_pb2.Dataset | parquet.DatasetView,
) -> set[str]:
    """Returns a set containing the reaction IDs in a Dataset.

    For ``DatasetView``, the ``reaction_id`` column is read directly from the Parquet
    file so we never decode Reaction blobs just to collect IDs.
    """
    if isinstance(dataset, parquet.DatasetView):
        return {rid for rid in dataset.iter_reaction_ids() if rid}
    return {
        reaction.reaction_id for reaction in dataset.reactions if reaction.reaction_id
    }


def _dataset_suffix(filename: str) -> str:
    """Returns the suffix that determines a dataset file's serialization format.

    Args:
        filename: Dataset filename, e.g. ``data/4d/ord_dataset-abc.pb.gz``.

    Returns:
        The format-bearing suffix, keeping a trailing ``.gz``: e.g. ``.pb.gz``,
        ``.binpb``, or ``.parquet``. Empty if the filename has no suffix.
    """
    suffixes = pathlib.Path(filename).suffixes
    if not suffixes:
        return ""
    if suffixes[-1] == ".gz":
        return "".join(suffixes[-2:])
    return suffixes[-1]


def _load_base_dataset(
    file_status: FileStatus, base: str
) -> dataset_pb2.Dataset | parquet.DatasetView | None:
    """Loads a Dataset from another git branch.

    Parquet inputs are spilled to a temp file and wrapped in a ``DatasetView`` so the
    diff path can scan the ``reaction_id`` column without decoding any Reaction blobs.
    The temp file outlives the view (process-lifetime leak), which is fine for this CLI
    script — the OS reclaims it on exit.
    """
    if file_status.status.startswith("A"):
        return None  # Dataset only exists in the submission.
    # NOTE(kearnes): Use --no-pager to avoid a non-zero exit code.
    git_args = ["git", "--no-pager", "show"]
    if file_status.status.startswith("R"):
        git_args.append(f"{base}:{file_status.original_filename}")
    else:
        git_args.append(f"{base}:{file_status.filename}")
    logger.info("Running command: %s", " ".join(git_args))
    # (internal git command)
    serialized = subprocess.run(git_args, capture_output=True, check=True, text=False)
    if serialized.stdout.startswith(b"version"):
        # Convert Git LFS pointers to real data.
        serialized = subprocess.run(
            ["git", "lfs", "smudge"],
            input=serialized.stdout,
            capture_output=True,
            check=True,
            text=False,
        )
    suffix = _dataset_suffix(git_args[-1])
    if suffix == ".parquet":
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as temp:
            temp.write(serialized.stdout)
            temp_path = temp.name
        return parquet.DatasetView(temp_path)
    # load_message picks the parser from the suffix, so the temp file has to keep
    # it: .pb/.binpb parse as binary, .pbtxt/.txtpb as text, and a trailing .gz
    # decompresses either. This copy is read before the file is closed, so unlike
    # the Parquet view above it does not need to outlive the block.
    with tempfile.NamedTemporaryFile(suffix=suffix) as temp:
        temp.write(serialized.stdout)
        temp.flush()
        return message_helpers.load_message(temp.name, dataset_pb2.Dataset)


def get_change_stats(
    datasets: Mapping[str, dataset_pb2.Dataset | parquet.DatasetView | None],
    inputs: Iterable[FileStatus],
    base: str,
) -> tuple[set[str], set[str], set[str]]:
    """Computes diff statistics for the submission.

    Args:
        datasets: Dict mapping filenames to Dataset messages. Values may be None only
            for deleted files; non-deleted inputs must have a Dataset for `filename`.
        inputs: List of FileStatus objects.
        base: Git branch to diff against.

    Returns:
        added: Set of added reaction IDs.
        removed: Set of deleted reaction IDs.
        changed: Set of changed reaction IDs.
    """
    old, new = set(), set()
    for file_status in inputs:
        if not file_status.status.startswith("D"):
            current = datasets[file_status.filename]
            if current is None:
                raise ValueError(
                    f"missing dataset for non-deleted file: {file_status.filename}"
                )
            new.update(_get_reaction_ids(current))
        dataset = _load_base_dataset(file_status, base)
        if dataset is not None:
            old.update(_get_reaction_ids(dataset))
    return new - old, old - new, new & old


def _run_updates(
    datasets: Mapping[str, dataset_pb2.Dataset | parquet.DatasetView],
    *,
    root: str,
    output_format: str,
    write_errors: bool,
    cleanup_files: bool,
) -> None:
    """Updates the submission files.

    When the input is a ``DatasetView`` and the output is also Parquet, the update runs
    as a streaming two-pass over the input file with an atomic temp-then-rename publish
    (validation runs against the temp before the rename). Otherwise the in-memory path
    mutates the Dataset in place, validates, and writes through
    ``ord_schema.datasets.save_dataset``.
    """
    options = validations.ValidationOptions(validate_ids=True, require_provenance=True)
    for input_filename, dataset in datasets.items():
        is_parquet_stream = (
            isinstance(dataset, parquet.DatasetView) and output_format == ".parquet"
        )
        if is_parquet_stream:
            # Resolve dataset_id up-front so the output filename is known
            # before we open the streaming writer.
            updates.assign_dataset_id(dataset)
            output_filename = str(
                pathlib.Path(root)
                / message_helpers.id_filename(f"{dataset.dataset_id}{output_format}")
            )
            pathlib.Path(output_filename).parent.mkdir(parents=True, exist_ok=True)
            # Atomic publish: stream-write to a sibling temp, validate it,
            # and let atomic_path os.replace it onto output_filename on clean
            # exit (or unlink it on failure). Cleanup runs after the publish
            # so output_filename is guaranteed to exist before we touch
            # git's index.
            #
            # Note: DatasetWriter inside update_parquet_dataset opens its own
            # mkstemp temp next to ``temp_filename``, then renames onto it
            # before atomic_path renames onto output_filename. Two atomic
            # rename hops per successful write; if the process dies between
            # them only the orphan inner temp is left behind.
            with atomic_io.atomic_path(output_filename) as temp_filename:
                updates.update_parquet_dataset(
                    input_filename, temp_filename, dataset_id=dataset.dataset_id
                )
                validations.validate_datasets(
                    {input_filename: parquet.DatasetView(temp_filename)},
                    write_errors,
                    options=options,
                )
                logger.info("writing Dataset to %s", output_filename)
            if cleanup_files:
                cleanup(input_filename, output_filename)
            continue
        # In-memory path: materialize a Parquet input if the requested output
        # format is not Parquet (so we can mutate via update_dataset).
        if isinstance(dataset, parquet.DatasetView):
            # (materialize the view in place)
            dataset = parquet.load_dataset(input_filename)  # noqa: PLW2901
        updates.update_dataset(dataset)
        validations.validate_datasets(
            {input_filename: dataset}, write_errors, options=options
        )
        output_filename = str(
            pathlib.Path(root)
            / message_helpers.id_filename(f"{dataset.dataset_id}{output_format}")
        )
        pathlib.Path(output_filename).parent.mkdir(parents=True, exist_ok=True)
        if cleanup_files:
            cleanup(input_filename, output_filename)
        logger.info("writing Dataset to %s", output_filename)
        save_dataset(dataset, output_filename)


def run(
    args: argparse.Namespace,
) -> tuple[set[str] | None, set[str] | None, set[str] | None]:
    """Main function that returns added/removed reaction ID sets.

    This function should be called directly by tests to get access to the
    return values. If main() returns something other than None it will break
    shell error code logic downstream.

    Returns:
        added: Set of added reaction IDs.
        removed: Set of deleted reaction IDs.
        changed: Set of changed reaction IDs.
    """
    inputs = sorted(_get_inputs(args))
    if not inputs:
        logger.info("nothing to do")
        return set(), set(), set()  # Nothing to do.
    # NOTE(kearnes): Process one dataset at a time to avoid OOM errors.
    change_stats = {}
    # Collected rather than raised on the spot so that a submission of several
    # files reports every invalid one. Raising at the first would make a
    # contributor fix, push, and wait once per bad file to discover the rest.
    validation_errors: list[str] = []
    for file_status in inputs:
        dataset: dataset_pb2.Dataset | parquet.DatasetView | None
        if file_status.status == "D":
            dataset = None
        elif file_status.filename.endswith(".parquet"):
            dataset = parquet.DatasetView(file_status.filename)
            logger.info(
                "%s: %d reactions", file_status.filename, len(dataset.reactions)
            )
        else:
            dataset = message_helpers.load_message(
                file_status.filename, dataset_pb2.Dataset
            )
            logger.info(
                "%s: %d reactions", file_status.filename, len(dataset.reactions)
            )
        datasets: dict[str, dataset_pb2.Dataset | parquet.DatasetView | None] = {
            file_status.filename: dataset
        }
        datasets_checked: dict[str, dataset_pb2.Dataset | parquet.DatasetView] = (
            {file_status.filename: dataset} if dataset is not None else {}
        )
        is_valid = True
        if not args.no_validate and dataset is not None:
            try:
                # Note: this does not check if IDs are malformed.
                validations.validate_datasets(datasets_checked, args.write_errors)
            except validations.ValidationError as error:
                validation_errors.append(f"{file_status.filename}: {error}")
                is_valid = False
            else:
                # Check reaction sizes. Left to raise where it stands: it is a
                # hard limit rather than a data defect, and one oversized
                # reaction says nothing useful about the remaining files.
                for reaction in dataset.reactions:
                    reaction_size = sys.getsizeof(reaction.SerializeToString()) / 1e6
                    if reaction_size > args.max_size:
                        raise ValueError(
                            f"Reaction is larger than --max_size "
                            f"({reaction_size} vs {args.max_size})"
                        )
        if args.base:
            added, removed, changed = get_change_stats(
                datasets, [file_status], base=args.base
            )
            change_stats[file_status.filename] = (added, removed, changed)
            logger.info(
                "Summary: +%d -%d Δ%d reaction IDs",
                len(added),
                len(removed),
                len(changed),
            )
        if args.update and dataset is not None and is_valid:
            # An explicit --output_format always wins; that is what it is for,
            # including converting an existing dataset on purpose. Absent one,
            # a new submission lands in the standard format and a dataset
            # already in the repository keeps the one it has -- rewriting an
            # edited .pb.gz as Parquet would rename a published path and delete
            # the .pb.gz as a side effect of correcting one dataset, which is
            # convert_to_parquet.py's decision to make over the whole corpus.
            if args.output_format is not None:
                output_format = args.output_format
            elif file_status.status.startswith("A"):
                output_format = DEFAULT_OUTPUT_FORMAT
            else:
                output_format = (
                    _dataset_suffix(file_status.filename) or DEFAULT_OUTPUT_FORMAT
                )
            _run_updates(
                datasets_checked,
                root=args.root,
                output_format=output_format,
                write_errors=args.write_errors,
                cleanup_files=args.cleanup,
            )
    if validation_errors:
        raise validations.ValidationError(
            f"validation encountered errors in {len(validation_errors)} of "
            f"{len(inputs)} files:\n" + "\n".join(validation_errors)
        )
    if change_stats:
        total_added, total_removed, total_changed = set(), set(), set()
        comment = [
            "Change summary:",
            "| Filename | Added | Removed | Changed |",
            "| -------- | ----- | ------- | ------- |",
        ]
        for filename, (added, removed, changed) in change_stats.items():
            comment.append(
                f"| {filename} | {len(added)} | {len(removed)} | {len(changed)} |"
            )
            total_added |= added
            total_removed |= removed
            total_changed |= changed
        comment.append(
            f"| | **{len(total_added)}** | **{len(total_removed)}** | "
            f"**{len(total_changed)}** |"
        )
        if args.issue and args.token:
            client = github.Github(args.token)
            repo = client.get_repo(os.environ["GITHUB_REPOSITORY"])
            issue = repo.get_issue(int(args.issue))
            issue.create_comment("\n".join(comment))
    else:
        total_added, total_removed, total_changed = None, None, None
    return total_added, total_removed, total_changed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Process datasets for database submissions"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--input_pattern", default=None, help="Pattern matching input Dataset protos"
    )
    group.add_argument(
        "--input_file", default=None, help="File containing Dataset proto filenames"
    )
    parser.add_argument("--root", default="", help="Root of the repository")
    parser.add_argument(
        "--output_format",
        default=None,
        help=(
            f"Dataset output format. Defaults to {DEFAULT_OUTPUT_FORMAT} for a new "
            "submission and to a dataset's existing format when one is edited; "
            "setting this overrides both."
        ),
    )
    parser.add_argument(
        "--write_errors",
        action="store_true",
        help="If True, errors will be written to *.error",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="If set, reactions will not be validated",
    )
    parser.add_argument(
        "--update", action="store_true", help="If True, update Reaction protos"
    )
    parser.add_argument(
        "--cleanup", action="store_true", help="If True, use git to clean up"
    )
    parser.add_argument(
        "--max_size",
        type=float,
        default=10.0,
        help="Maximum size (in MB) for any Reaction message",
    )
    parser.add_argument("--base", default=None, help="Git branch to diff against")
    parser.add_argument("--issue", default=None, help="GitHub pull request number")
    parser.add_argument("--token", default=None, help="GitHub authentication token")
    return parser.parse_args(argv)


def main(args: argparse.Namespace) -> None:
    """Silences RDKit logs and runs dataset processing for a database submission."""
    silence_rdkit_logs()
    run(args)


if __name__ == "__main__":
    main(parse_args())
