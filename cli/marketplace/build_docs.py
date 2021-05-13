import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Union, Optional, Set

import click
import yaml
from bs4 import BeautifulSoup
from sphinx.cmd.build import main as sphinx_build_cmd
from sphinx.ext.apidoc import main as sphinx_apidoc_cmd

from cli.helpers import (
    is_item_dir,
    render_jinja_file,
    PROJECT_ROOT,
    get_item_yaml_requirements,
)
from cli.marketplace.changelog import ChangeLog
from cli.path_iterator import PathIterator

_verbose = False


@click.command()
@click.option("-s", "--source-dir", help="Path to the source directory")
@click.option("-t", "--target-dir", help="Path to output directory")
@click.option(
    "-T", "--temp-dir", default="/tmp", help="Path to intermediate build directory"
)
@click.option("-c", "--channel", default="master", help="Name of build channel")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    default=False,
    help="When this flag is set, the process will output extra information",
)
def build_docs(
    source_dir: str, target_dir: str, temp_dir: str, channel: str, verbose: bool
):
    global _verbose
    _verbose = verbose

    root_base = Path(temp_dir) / uuid.uuid4().hex
    temp_root = root_base / "functions"
    temp_docs = root_base / "docs"

    source_dir = Path(source_dir).resolve()
    target_dir = Path(target_dir).resolve()

    target_channel = target_dir / channel

    temp_root.mkdir(parents=True)
    temp_docs.mkdir(parents=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_channel.mkdir(parents=True, exist_ok=True)

    click.echo(f"Temporary working directory: {root_base}")

    if _verbose:
        print_file_tree("Current marketplace structure", target_channel)

    requirements = collect_temp_requirements(source_dir)
    sphinx_quickstart(temp_docs, requirements)

    build_temp_project(source_dir, temp_root)
    build_temp_docs(temp_root, temp_docs)
    patch_temp_docs(source_dir, temp_docs)

    if _verbose:
        print_file_tree("Temporary project structure", temp_root)

    render_html_files(temp_docs)

    change_log = ChangeLog()
    copy_static_resources(target_dir, temp_docs)

    update_or_create_items(change_log, source_dir, target_channel, temp_docs)
    build_catalog_json(target_channel)

    if _verbose:
        print_file_tree("Resulting marketplace structure", target_channel)

    write_change_log(target_dir / "README.md", change_log)


def print_file_tree(title: str, path: Union[str, Path]):
    click.echo(f"\n\n -- {title}:")
    path = Path(path)
    lines = ["---------------------------------", f"\t{path.resolve()}"]
    for file in path.iterdir():
        lines.append("\t|")
        lines.append(f"\t|__ {file.name}")
        if file.is_dir():
            for sub_path in file.iterdir():
                lines.append("\t|\t|")
                lines.append(f"\t|\t|__ {sub_path.name}")
    lines.append("---------------------------------")
    click.echo("\n".join(lines))
    click.echo("\n\n")


def write_change_log(readme: Path, change_log: ChangeLog):
    readme.touch(exist_ok=True)
    content = open(readme, "r").read()
    with open(readme, "w") as f:
        if change_log.changes_available:
            compiled_change_log = change_log.compile()
            f.write(compiled_change_log)
        f.write(content)


def copy_static_resources(target_dir, temp_docs):
    target_static = target_dir / "_static"
    if not target_static.exists():
        click.echo("Copying static resources...")
        shutil.copytree(temp_docs / "_build/_static", target_static)


def update_or_create_items(change_log, source_dir, target_dir, temp_docs):
    click.echo("Creating items...")
    for directory in PathIterator(root=source_dir, rule=is_item_dir, as_path=True):
        update_or_create_item(directory, target_dir, temp_docs, change_log)


def build_catalog_json(target_dir: Union[str, Path]):
    click.echo("Building catalog.json...")
    target_dir = Path(target_dir)
    catalog_path = target_dir / "catalog.json"
    catalog = json.load(open(catalog_path, "r")) if catalog_path.exists() else {}

    for source_dir in target_dir.iterdir():
        if not source_dir.is_dir() or source_dir.name == "_static":
            continue

        latest_dir = source_dir / "latest"
        source_yaml_path = latest_dir / "item.yaml"

        latest_yaml = yaml.full_load(open(source_yaml_path, "r"))
        latest_yaml["generationDate"] = str(latest_yaml["generationDate"])

        latest_version = latest_yaml["version"]

        catalog[source_dir.name] = {"latest": latest_yaml}
        for version_dir in source_dir.iterdir():
            version = version_dir.name

            if version != "latest" and version != latest_version:
                version_yaml_path = version_dir / "item.yaml"
                version_yaml = yaml.full_load(open(version_yaml_path, "r"))
                version_yaml["generationDate"] = str(version_yaml["generationDate"])
                catalog[source_dir.name][version] = version_yaml

    json.dump(catalog, open(catalog_path, "w"))


def update_or_create_item(
    source_dir: Path, target: Path, temp_docs: Path, change_log: ChangeLog
):
    # Copy source directories to target directories, if target already has the directory, archive previous version
    source_yaml = yaml.full_load(open(source_dir / "item.yaml", "r"))
    source_version = source_yaml["version"]

    target_dir = target / source_dir.stem
    target_latest = target_dir / "latest"
    target_version = target_dir / source_version

    if target_version.exists():
        click.echo("Source version already exists in target directory!")
        return

    build_path = temp_docs / "_build"
    source_html_name = f"{source_dir.stem}.html"
    example_html_name = f"{source_dir.stem}_example.html"

    source_html = build_path / source_html_name
    update_html_resource_paths(source_html, relative_path="../../../")

    example_html = build_path / example_html_name
    update_html_resource_paths(example_html, relative_path="../../../")

    # If its the first source is encountered, copy source to target
    if target_dir.exists():
        shutil.rmtree(target_latest)
        change_log.update_item(source_dir.stem, source_version, target_version.name)
    else:
        change_log.new_item(source_dir.stem, source_version)

    shutil.copytree(source_dir, target_latest)
    shutil.copytree(source_dir, target_version)

    if source_html.exists():
        shutil.copy(source_html, target_latest / source_html_name)
        shutil.copy(source_html, target_version / source_html_name)

    if example_html.exists():
        shutil.copy(example_html, target_latest / example_html_name)
        shutil.copy(example_html, target_version / example_html_name)


def update_html_resource_paths(html_path: Path, relative_path: str):
    if html_path.exists():
        with open(html_path, "r") as html:
            parsed = BeautifulSoup(html.read(), features="html.parser")

        nodes = parsed.find_all(
            lambda node: node.name == "link" and "_static" in node.get("href", "")
        )
        for node in nodes:
            node["href"] = f"{relative_path}{node['href']}"

        nodes = parsed.find_all(
            lambda node: node.name == "script"
            and node.get("src", "").startswith("_static")
        )
        for node in nodes:
            node["src"] = f"{relative_path}{node['src']}"

        with open(html_path, "w") as new_html:
            new_html.write(str(parsed))


def render_html_files(temp_docs):
    cmd = f"-b html {temp_docs} {temp_docs / '_build'}"
    click.echo(f"Rendering HTML... [sphinx {cmd}]")
    sphinx_build_cmd(cmd.split(" "))


def patch_temp_docs(source_dir, temp_docs):
    click.echo("Patching temporary docs...")

    for directory in PathIterator(root=source_dir, rule=is_item_dir):
        directory = Path(directory)
        with open(directory / "item.yaml", "r") as f:
            item = yaml.full_load(f)

        example_file = directory / item["example"]
        shutil.copy(example_file, temp_docs / f"{directory.name}_example.ipynb")


def build_temp_project(source_dir, temp_root):
    click.echo("[Temporary project] Starting to build project...")

    if _verbose:
        click.echo(f"Source dir: {source_dir}")
        click.echo(f"Temp root: {temp_root}")

    item_count = 0
    for directory in PathIterator(root=source_dir, rule=is_item_dir, as_path=True):
        if _verbose:
            item_count += 1
            click.echo(f"[Temporary project] Now processing: {directory / 'item.yaml'}")

        with open(directory / "item.yaml", "r") as f:
            item = yaml.full_load(f)

        py_file = directory / item.get("spec")["filename"]

        temp_dir = temp_root / directory.name
        temp_dir.mkdir(parents=True, exist_ok=True)

        (temp_dir / "__init__.py").touch()
        shutil.copy(py_file, temp_dir / py_file.name)

    if _verbose:
        click.echo(f"[Temporary project] Done project (item count: {item_count})")


def collect_temp_requirements(source_dir) -> Set[str]:
    click.echo("[Temporary project] Starting to collect requirements...")
    requirements = set()

    for directory in PathIterator(root=source_dir, rule=is_item_dir, as_path=True):
        item_requirements = get_item_yaml_requirements(directory)
        for item_requirement in item_requirements:
            requirements.add(item_requirement)

    if _verbose:
        click.echo(
            f"[Temporary project] Done requirements ({', '.join(requirements)})"
        )

    return requirements


def sphinx_quickstart(
    temp_root: Union[str, Path], requirements: Optional[Set[str]] = None
):
    click.echo("[Sphinx] Running quickstart...")

    subprocess.run(
        f"sphinx-quickstart --no-sep -p Marketplace -a Iguazio -l en -r '' {temp_root}",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
    )

    requirements = requirements or ""
    if requirements:
        requirements = '", "'.join(requirements)
        requirements = f'"{requirements}"'

    conf_py_target = temp_root / "conf.py"
    conf_py_target.unlink()

    render_jinja_file(
        template_path=PROJECT_ROOT / "cli" / "marketplace" / "conf.template",
        output_path=conf_py_target,
        data={
            "sphinx_docs_target": temp_root,
            "repository_url": "https://github.com/mlrun/marketplace",
            "mock_imports": requirements,
        },
    )

    click.echo("[Sphinx] Done quickstart")


def build_temp_docs(temp_root, temp_docs):
    click.echo("[Sphinx] Running autodoc...")

    cmd = f"-F -o {temp_docs} {temp_root}"
    click.echo(f"Building temporary sphinx docs... [sphinx-apidoc {cmd}]")

    sphinx_apidoc_cmd(cmd.split(" "))

    click.echo("[Sphinx] Done autodoc")


if __name__ == "__main__":
    build_docs()
