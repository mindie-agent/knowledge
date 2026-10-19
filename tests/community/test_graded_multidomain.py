"""Independent semantic domains and feed targets through real local Git/RPC.

FileTransport supplies only the GitHub API boundary. These synthetic units are
mechanism evidence, not published corpus coverage or hardware claims.
"""
from contextlib import ExitStack, closing
import json
import threading

import pytest

from graded_fixture import OBSERVED, unit
from mindie_knowledge.community.common import Deadline
from mindie_knowledge.community.transport import FileTransport
from mindie_knowledge.graded.maintenance import build_routing_index
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.domain_routes import select_engine_config
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.settings import load
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transport import Service, rpc
from mindie_knowledge.materials.references import ReadReferenceError
from mindie_knowledge.publication_contract import GRADED_SCHEMA, make_contract, render_contract

from .conftest import git, make_remote
from .test_graded_cycle import commit, latest


def corpus(tmp_path, name, domain):
    remote = make_remote(tmp_path, name)
    work = tmp_path / (name + "-maintainer")
    git(["clone", remote, str(work)])
    (work / "publication-contract.json").write_text(render_contract(make_contract(domain, "a" * 40, schema=GRADED_SCHEMA)))
    commit(work, "Declare isolated semantic domain")
    return remote, work


def configured(tmp_path, settings, name, repository, remote, *, generation="g1"):
    path = tmp_path / (name + "-settings.json")
    value = {key: val for key, val in settings.items() if key != "config_path"}
    value.update(repository=repository, dev_remotes={repository: remote}, generation=generation)
    path.write_text(json.dumps(value))
    return path


def source(store, repository, remote):
    return Feed(store, dict(domain=store.domain, repository=repository, ref="main", url=remote))


def merge(state, repository, remote, row):
    transport = FileTransport(state / "dev-github.json", {repository: remote})
    number = int(row["pr_url"].rsplit("/", 1)[1])
    return transport.merge_pull_request(repository, number, sha=row["head_sha"], method="squash", deadline=Deadline(20, 10))


def publish(engine, repository, remote):
    engine.publisher.flush()
    row = latest(engine.store)
    assert row["status"] == "submitted", row["detail"]
    merge(engine.state_dir, repository, remote, row)
    return row


def test_two_domains_same_unit_id_update_feedback_and_withdraw_independently(tmp_path, settings):
    domains = ("operators", "vllm-ascend")
    records, configs, services, threads = {}, {}, {}, []
    with ExitStack() as stack:
        for domain in domains:
            repository = "fixture/" + domain
            remote, maintainer = corpus(tmp_path, domain, domain)
            producer = stack.enter_context(closing(Store(tmp_path / "producer", domain)))
            consumer = stack.enter_context(closing(Store(tmp_path / "consumer", domain)))
            config = configured(tmp_path, settings, domain, repository, remote)
            engine = Engine(producer, settings_path=config, state_dir=tmp_path / "publisher" / domain)
            producer_feed, consumer_feed = source(producer, repository, remote), source(consumer, repository, remote)
            assert producer_feed.sync(force=True)["status"] == "synced"
            value = unit("unit-shared-mechanism", body=f"Synthetic {domain} graph mechanism.")
            producer.graded.install([value], generation="g1")
            publish(engine, repository, remote)
            producer_feed.sync(force=True)
            consumer_feed.sync(force=True)
            admission = Admission(tmp_path / (domain + "-admission.sqlite3"))
            service = Service(Engine(consumer), admission=admission, feeds=[consumer_feed])
            thread = threading.Thread(target=service.http.serve_forever, daemon=True)
            thread.start()
            threads.append((thread, service))
            config = tmp_path / (domain + "-engine.json")
            config.write_text(json.dumps(dict(root=str(tmp_path / "consumer"), domain=domain)))
            configs[domain] = str(config)
            services[config.resolve()] = service
            records[domain] = dict(producer=producer, consumer=consumer, engine=engine, source=producer_feed,
                                   feed=consumer_feed, remote=remote, repository=repository, maintainer=maintainer, unit=value)
        try:
            default = configs["operators"]
            identity = dict(_session_id="verified-native-session", _session_verified=True)

            def call(method, *, domain=None, **args):
                selected = select_engine_config(default, domain=domain, ref=args.get("ref"), domain_configs=configs)
                if domain is not None:
                    args["domain"] = domain
                return rpc(services[selected].connection, method, {**args, **identity})

            default_result = call("query", query="graph", conditions=OBSERVED)["results"][0]
            selected_result = call("query", domain="vllm-ascend", query="graph", conditions=OBSERVED)["results"][0]
            assert default_result["id"] == selected_result["id"]
            assert default_result["ref"] != selected_result["ref"]
            assert "operators" in call("explain", ref=default_result["ref"])["content"]
            assert "vllm-ascend" in call("explain", ref=selected_result["ref"])["content"]
            with pytest.raises(ValueError, match="conflicts"):
                call("explain", domain="operators", ref=selected_result["ref"])
            with pytest.raises(ValueError, match="domain differs"):
                services[select_engine_config(default)].call("query", {**identity, "domain": "vllm-ascend", "query": "graph"})

            operators, vllm = records["operators"], records["vllm-ascend"]
            operators["consumer"].record_vote(root_hash="observed-root", ref=default_result["feedback_ref"],
                rating="up", reason="Synthetic operators mechanism reused.", publishable=True, generation="g2")
            vote_config = configured(tmp_path, settings, "operators-voter", operators["repository"], operators["remote"], generation="g2")
            voter = Engine(operators["consumer"], settings_path=vote_config, state_dir=tmp_path / "voter")
            untouched = git(["show-ref"], cwd=vllm["remote"])
            publish(voter, operators["repository"], operators["remote"])
            operators["feed"].sync(force=True)
            assert call("explain", ref=default_result["ref"])["feedback"]["up"] == 1
            assert call("explain", ref=selected_result["ref"])["feedback"]["up"] == 0
            assert git(["show-ref"], cwd=vllm["remote"]) == untouched

            operators["source"].sync(force=True)
            changed = GradedUnit.from_parts(operators["unit"].metadata,
                operators["unit"].body + "Reviewed operators correction.\n", operators["unit"].path)
            operators["producer"].graded.install([changed], generation="g1")
            publish(operators["engine"], operators["repository"], operators["remote"])
            operators["feed"].sync(force=True)
            current = call("query", query="graph", conditions=OBSERVED)["results"][0]
            assert current["revision"] == 2 and current["feedback"]["up"] == 0
            assert call("explain", ref=selected_result["ref"])["revision"] == 1
            assert git(["show-ref"], cwd=vllm["remote"]) == untouched
            with pytest.raises(ReadReferenceError):
                operators["consumer"].explain(default_result["ref"])

            git(["pull", "--ff-only", "origin", "main"], cwd=vllm["maintainer"])
            (vllm["maintainer"] / vllm["unit"].path).unlink()
            assert build_routing_index(vllm["maintainer"])["ok"]
            commit(vllm["maintainer"], "Withdraw only vllm-ascend mechanism")
            vllm["feed"].sync(force=True)
            assert call("query", domain="vllm-ascend", query="graph", conditions=OBSERVED)["results"] == []
            assert call("explain", ref=current["ref"])["revision"] == 2
            assert operators["consumer"].db.execute("SELECT count(*) FROM votes").fetchone()[0] == 1
            assert vllm["consumer"].db.execute("SELECT count(*) FROM votes").fetchone()[0] == 0
        finally:
            for thread, service in threads:
                service.http.shutdown()
                service.http.server_close()
                thread.join(timeout=2)


def test_same_domain_multifeed_export_uses_target_tree_and_frozen_receipts(tmp_path, settings):
    target_repo, other_repo = "fixture/operators-target", "fixture/operators-reference"
    target_remote, _ = corpus(tmp_path, "target", "operators")
    other_remote, other_work = corpus(tmp_path, "reference", "operators")
    reference = unit("unit-reference-only")
    path = other_work / reference.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(reference.render())
    build_routing_index(other_work)
    commit(other_work, "Install independent reference source")
    config = configured(tmp_path, settings, "target", target_repo, target_remote)
    with closing(Store(tmp_path / "multi", "operators")) as store:
        target, other = source(store, target_repo, target_remote), source(store, other_repo, other_remote)
        target.sync(force=True)
        target_commit = git(["rev-parse", "main"], cwd=target_remote)
        other.sync(force=True)  # Last sync must not choose the publication base.
        store.graded.install([unit("unit-target-new")], generation="g1")
        seen = store.explain(store.graded.ref(reference.id))
        store.record_vote(root_hash="reader", ref=seen["feedback_ref"], rating="up", reason="Read reference source.",
                          publishable=True, generation="g1")
        built = build_batch(store, settings=load(config))
        assert built[2]["base_commit"] == target_commit
        assert not any(item["path"].startswith("feedback/") for item in built[2]["files"])
        manifest = json.loads(next(item["content"] for item in built[2]["files"] if item["path"] == "index/routing-manifest.json"))
        assert {item["id"] for item in manifest["units"]} == {"unit-target-new"}
        engine = Engine(store, settings_path=config, state_dir=tmp_path / "target-publisher")
        receipt = engine.publisher.submit(store.batch(built[0]))
        assert receipt["status"] == "submitted", receipt
        saved = store.feed_get("sent-graded-derived:" + target.ident)
        assert saved["source_commit"] == target_commit
        assert store.feed_get("sent-graded-derived:" + other.ident) is None
        assert store.db.execute("SELECT repository FROM sent_receipts").fetchone()[0] == target_repo
        assert len(store.unbatched_votes(generation="g1")) == 1


def test_frozen_batch_cannot_redirect_after_repository_setting_changes(tmp_path, settings):
    target_repo, other_repo = "fixture/original", "fixture/different"
    target_remote, _ = corpus(tmp_path, "original", "operators")
    other_remote, _ = corpus(tmp_path, "different", "operators")
    config = configured(tmp_path, settings, "frozen", target_repo, target_remote)
    with closing(Store(tmp_path / "producer", "operators")) as store:
        source(store, target_repo, target_remote).sync(force=True)
        store.graded.install([unit()], generation="g1")
        built = build_batch(store, settings=load(config))
        value = json.loads(config.read_text())
        value.update(repository=other_repo, dev_remotes={other_repo: other_remote})
        config.write_text(json.dumps(value))
        before = {remote: git(["show-ref"], cwd=remote) for remote in (target_remote, other_remote)}
        engine = Engine(store, settings_path=config, state_dir=tmp_path / "publisher")
        engine.publisher.submit(store.batch(built[0]))
        assert store.batch(built[0])["status"] == "needs_review"
        assert all(git(["show-ref"], cwd=remote) == heads for remote, heads in before.items())
        assert not (engine.state_dir / "dev-github.json").exists()
