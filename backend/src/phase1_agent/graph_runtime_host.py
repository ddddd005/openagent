"""Application wiring for public package capabilities and artifact authority."""

from contextlib import closing
from copy import deepcopy

from .graph_records import require
from .graph_store import GraphRecordStore
from .runtime_fact_store import RuntimeFactStore


class GraphRuntimeHost:
    """Combine host services; business algorithms remain in their packages."""

    @staticmethod
    def _runtime_descriptor(entry):
        descriptor = entry.definition.to_dict(
            executable=entry.executor is not None or entry.executor_ref is not None)
        if entry.executor_ref is not None:
            descriptor["executor_ref"] = entry.executor_ref.to_dict()
        return descriptor

    @staticmethod
    def _runtime_facts(store, chain_id, *, node_run_id=None):
        return RuntimeFactStore(store).list(chain_id, node_run_id=node_run_id)

    def _accept_public_outputs(self, sid, chain_id, event):
        frame = self._service_runs.get(chain_id)
        if event["event"] == "succeeded" and frame is not None:
            published = {key: event[key] for key in (
                "event", "node_binding_id", "node_run_id", "outputs", "output_refs", "settlement_committed",
            ) if key in event}
            frame.accept_outputs({**published, "workflow_session_id": sid, "chain_run_id": chain_id})

    def _resolve_runtime_input(self, context, port):
        return self._resolve_runtime_input_detail(context, port)["value"]

    def _resolve_runtime_input_detail(self, context, port):
        refs = context.input_artifact_refs(port)
        require(len(refs) == 1, "runtime_input_unbound",
                "Capability requires one exact accepted input", 409)
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            run = repo.get("node_run", run_id=context.node_run_id)
            require(run["input_refs"].get(port) == refs, "runtime_input_unbound",
                    "Input differs from the accepted invocation binding", 409)
            output = repo.get("workflow_output", output_id=refs[0]["output_id"])
            producer = repo.get("node_run", run_id=output["run_id"])
            require(output["workflow_session_id"] == context.workflow_session_id
                    and output["chain_run_id"] == context.chain_run_id
                    and producer["status"] == "succeeded"
                    and producer["output_refs"].get(output["port_id"]) == output["output_id"],
                    "runtime_input_unbound", "Input is not accepted in this invocation", 409)
            return {"value": deepcopy(output["payload"]), "output_id": output["output_id"],
                    "producer": {"workflow_session_id": output["workflow_session_id"],
                                 "chain_run_id": output["chain_run_id"],
                                 "node_binding_id": output["node_binding_id"],
                                 "node_run_id": output["run_id"]}}

    def _describe_runtime_input_origin(self, context, port):
        """Describe one exact bound input without granting a selectable run lookup."""
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            consumer = repo.get("node_run", run_id=context.node_run_id)
            session = repo.get("workflow_session", workflow_session_id=context.workflow_session_id)
            chain = repo.get("chain_run", chain_run_id=context.chain_run_id)
            require(consumer["workflow_session_id"] == context.workflow_session_id
                    and consumer["chain_run_id"] == context.chain_run_id
                    and consumer["node_binding_id"] == context.node_binding_id
                    and consumer["status"] == "running"
                    and chain["status"] == "running"
                    and session["active_chain_run_id"] == context.chain_run_id,
                    "runtime_origin_owner_mismatch", "Input origin caller is no longer this running invocation", 409)
            detail = self._resolve_runtime_input_detail(context, port)
            output = repo.get("workflow_output", output_id=detail["output_id"])
            producer = repo.get("node_run", run_id=output["run_id"])
            document = self._document_for_runtime_output(repo, output)
            declaration = next((node for node in document["nodes"]
                                if node["node_binding_id"] == output["node_binding_id"]), None)
            require(declaration is not None, "storage_contract_violation",
                    "Accepted input producer has no frozen declaration", 500)
            return {"output_id": detail["output_id"], "producer": detail["producer"],
                    "component_id": declaration["component_id"],
                    "component_version": declaration["component_version"],
                    "input_refs": deepcopy(producer["input_refs"])}

    def _accept_runtime_fact(self, owner, fact, stream_id):
        # A fact is its own immutable idempotency record. Avoid copying its
        # body into a management receipt or scanning all historical graphs.
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo, facts = GraphRecordStore(store), RuntimeFactStore(store)
            try:
                store._connection.execute("BEGIN IMMEDIATE")
                session = repo.get("workflow_session", workflow_session_id=owner["workflow_session_id"])
                chain = repo.get("chain_run", chain_run_id=owner["chain_run_id"])
                run = repo.get("node_run", run_id=owner["node_run_id"])
                require(session["active_chain_run_id"] == owner["chain_run_id"]
                        and chain["status"] == "running"
                        and run["chain_run_id"] == owner["chain_run_id"]
                        and run["node_binding_id"] == owner["node_binding_id"]
                        and run["status"] == "running",
                        "execution_owner_changed", "Runtime fact owner is no longer running", 409)
                receipt = facts.accept(owner=owner, stream_id=stream_id, fact=fact)
                store._inject("before_runtime_fact_commit")
                store._connection.execute("COMMIT")
                return receipt
            except BaseException:
                if store._connection.in_transaction:
                    store._connection.execute("ROLLBACK")
                raise

    def _accept_service_fact(self, context, fact, stream_id):
        owner = {"workflow_session_id": context.workflow_session_id,
                 "chain_run_id": context.chain_run_id,
                 "node_binding_id": context.node_binding_id, "node_run_id": context.node_run_id}
        return self._accept_runtime_fact(owner, fact, stream_id)

    def _prepare_public_capabilities(self, sid, chain_id, plan, resources):
        from .host_services import HostServiceEnvironment, HostServiceRun
        nodes = {node["node_binding_id"]: node for node in plan.document["nodes"]
                 if node["node_binding_id"] in plan.ordered_node_ids}
        frame = HostServiceRun(self.registry.services, workflow_session_id=sid, chain_run_id=chain_id,
                               nodes=nodes, definitions={key: plan.definitions[key] for key in nodes})
        self._service_runs[chain_id] = frame

        def environment(reference, records):
            allowed_resources = {(record["scope"], record["type_id"], record["resource_id"])
                                 for items in records.values() for record in items}

            def live():
                require(self._service_runs.get(chain_id) is frame and not frame.released,
                        "host_service_unavailable", "Service environment has been released", 409)

            def authorize(context):
                live()
                frame.require_context(context)
                require(any(ref == reference for ref, _ in frame.routes[context.node_binding_id].values()),
                        "host_service_owner_mismatch", "Node has no grant to this service", 403)

            def resolve_input(context, port):
                authorize(context)
                return self._resolve_runtime_input_detail(context, port)

            def resolve_artifact(context, artifact):
                authorize(context)
                return self._resolve_runtime_artifact(context, artifact)

            def accept_fact(context, fact, stream_id):
                authorize(context)
                return self._accept_service_fact(context, fact, "service:" + reference.service_id + "@"
                                                 + reference.exact_version + ":" + stream_id)

            def read_resource(resource):
                from .host_sdk import ResourceIdentity
                live()
                identity = ResourceIdentity.from_dict(resource).to_dict()
                require((identity["scope"], identity["type_id"], identity["resource_id"]) in allowed_resources,
                        "graph_resource_dependency_undeclared",
                        "Service resource was not declared in this frozen run", 403)
                return self.get_global_resource(identity)

            options = {key: value if callable(value) else deepcopy(value)
                       for key, value in self._service_options.get(reference, {}).items()}
            return HostServiceEnvironment(resolve_input, resolve_artifact, accept_fact, read_resource, options)
        frame.prepare(environment, resources)

    def _release_public_capabilities(self, sid, chain_id):
        frame = self._service_runs.get(chain_id)
        if frame is not None and frame.release():
            self._service_runs.pop(chain_id, None)
        host = self._runtime_hosts.get(chain_id)
        if host is not None:
            with closing(self._store()) as store:
                from .runtime_hosting import InvocationOwner
                chain = GraphRecordStore(store).get("chain_run", chain_run_id=chain_id)
            for node_id, run_id in zip(chain["ordered_nodes"], chain["node_run_ids"]):
                owner = InvocationOwner(chain["workflow_session_id"], chain_id, node_id, run_id)
                if host.contains(owner):
                    host.release(owner)
            if host.active_handle_count == 0:
                self._runtime_hosts.pop(chain_id, None)
        self._release_information_chain(chain_id)

    def host_service_instances(self, chain_id):
        """Read-only instance observation for trusted application integrations."""
        frame = self._service_runs.get(chain_id)
        return () if frame is None else tuple(frame.instances.values())

    def _accept_executor_fact(self, envelope):
        from .runtime_hosting import InvocationOwner
        owner = InvocationOwner.from_dict(envelope["owner"])
        host = self._runtime_hosts.get(owner.chain_run_id)
        require(host is not None and host.accepts_fact_envelope(envelope),
                "runtime_stale_callback", "Executor callback is outside its active generation", 409)
        reference = envelope["executor_ref"]
        return self._accept_runtime_fact(
            owner.to_dict(), envelope, "executor:" + reference["executor_id"] + "@" + reference["exact_version"])

    def _host_for_chain(self, chain_id, registry):
        from .runtime_hosting import RuntimeHost
        if chain_id not in self._runtime_hosts:
            self._runtime_hosts[chain_id] = RuntimeHost(
                registry.executors, fact_sink=self._accept_executor_fact,
                information_router=self._information_router_for_chain(chain_id, registry),
                information_binding_sink=self._accept_information_bindings,
                information_component_resolver=self._information_component_for_owner)
        return self._runtime_hosts[chain_id]

    def _control_hosted_invocation(self, chain_id, action, command_id):
        """Never acquire the host lock while holding the service lock."""
        from .runtime_hosting import InvocationOwner
        with self._lock, closing(self._store()) as store:
            host = self._runtime_hosts.get(chain_id)
            if host is None:
                return
            chain = GraphRecordStore(store).get("chain_run", chain_run_id=chain_id)
            index = chain["next_node_index"]
            if index >= len(chain["node_run_ids"]):
                return
            owner = InvocationOwner(chain["workflow_session_id"], chain_id,
                                    chain["ordered_nodes"][index], chain["node_run_ids"][index])
        if not host.contains(owner):
            return
        snapshot = host.snapshot(owner)
        try:
            if action == "pause" and snapshot["status"] in ("ready", "running", "paused"):
                host.request_pause(owner, command_id)
            elif action == "resume" and snapshot["status"] == "paused":
                host.resume(owner, command_id)
        except Exception as error:
            if getattr(error, "reason_code", None) != "runtime_terminal_invocation":
                raise

    def _artifact_closure(self, repo, roots):
        """Walk registered references; never guess references from business fields."""
        pending = list(roots)
        seen = set()
        while pending:
            reference = pending.pop()
            if reference["scope"] != "artifact" or reference["output_id"] in seen:
                continue
            output_id = reference["output_id"]
            seen.add(output_id)
            require(len(seen) <= 8192, "runtime_reference_limit",
                    "Artifact reference closure exceeds the runtime limit", 409)
            output = repo.get("workflow_output", output_id=output_id)
            producer = repo.get("node_run", run_id=output["run_id"])
            require(producer["status"] == "succeeded"
                    and producer["output_refs"].get(output["port_id"]) == output_id,
                    "runtime_artifact_unaccepted", "Artifact has no accepted producer", 409)
            original = self._document_for_runtime_output(repo, output)
            node = next(node for node in original["nodes"]
                        if node["node_binding_id"] == output["node_binding_id"])
            entry = self.registry.get(node["component_id"], node["component_version"])
            require(entry is not None, "graph_missing_node_type",
                    "Artifact's registered implementation is unavailable", 409)
            port = next(port for port in entry.definition.ports(node["config"])[1]
                        if port.port_id == output["port_id"])
            pending.extend(self.registry.data_types.references(
                port.data_type, port.data_schema_version, output["payload"], scope="content"))
        return seen

    def _document_for_runtime_output(self, repo, output):
        chain = repo.get("chain_run", chain_run_id=output["chain_run_id"])
        return self._document(repo, chain["workflow_definition_id"], chain["definition_revision"])

    def _authorized_object_artifacts(self, repo, context):
        roots = []
        for read in context.reads:
            if read.get("kind") != "object_read":
                continue
            item = self._objects(repo).read(
                context.workflow_session_id, read["object_key"], node_id=context.node_binding_id,
                revision_id=read["revision_id"],
            )
            roots.extend(self.registry.data_types.references(
                item["type_id"], item["schema_version"], item["value"], scope="session"))
        return self._artifact_closure(repo, roots)

    def _validate_runtime_references(self, context, references):
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            roots = [{"scope": "artifact", "output_id": ref["output_id"]}
                     for refs in context._input_refs.values() for ref in refs]
            allowed = self._artifact_closure(repo, roots) | self._authorized_object_artifacts(repo, context)
            return all(ref["scope"] != "artifact" or ref["output_id"] in allowed for ref in references)

    def _resolve_runtime_artifact(self, context, reference):
        from .host_sdk import validate_reference
        reference = validate_reference(reference)
        require(reference["scope"] == "artifact", "runtime_artifact_reference_invalid",
                "Artifact resolution requires an exact artifact reference")
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            roots = [{"scope": "artifact", "output_id": ref["output_id"]}
                     for refs in context._input_refs.values() for ref in refs]
            allowed = self._artifact_closure(repo, roots) | self._authorized_object_artifacts(repo, context)
            require(reference["output_id"] in allowed, "runtime_artifact_reference_denied",
                    "Artifact is outside the input and authorized object reference closure", 403)
            output = repo.get("workflow_output", output_id=reference["output_id"])
            return {"value": deepcopy(output["payload"]), "output_id": output["output_id"],
                    "producer": {"workflow_session_id": output["workflow_session_id"],
                                 "chain_run_id": output["chain_run_id"],
                                 "node_binding_id": output["node_binding_id"], "node_run_id": output["run_id"]}}

    def _host_call(self, context, capability, operation, payload):
        frame = self._service_runs.get(context.chain_run_id)
        if frame is not None and frame.owns_capability(context, capability):
            with self._lock, closing(self._store()) as store:
                repo = GraphRecordStore(store)
                run = repo.get("node_run", run_id=context.node_run_id)
                session = repo.get("workflow_session", workflow_session_id=context.workflow_session_id)
                require(run["workflow_session_id"] == context.workflow_session_id
                        and run["chain_run_id"] == context.chain_run_id
                        and run["node_binding_id"] == context.node_binding_id
                        and run["status"] == "running"
                        and session["active_chain_run_id"] == context.chain_run_id,
                        "host_service_owner_mismatch", "Service call owner is no longer running", 409)
            return frame.call(context, capability, operation, payload)
        if capability == "artifacts:read" and operation == "resolve-input":
            require(type(payload) is dict and set(payload) == {"port"}
                    and type(payload["port"]) is str, "runtime_input_unbound",
                    "Artifact resolution requires an exact named input")
            return self._resolve_runtime_input_detail(context, payload["port"])
        if capability == "artifacts:read" and operation == "describe-input-origin":
            require(type(payload) is dict and set(payload) == {"port"} and type(payload["port"]) is str,
                    "runtime_origin_request_invalid", "Input origin requires only an exact input port")
            return self._describe_runtime_input_origin(context, payload["port"])
        if capability == "artifacts:read" and operation == "resolve-artifact":
            require(type(payload) is dict and set(payload) == {"reference"},
                    "runtime_artifact_reference_invalid", "Artifact resolution requires an exact reference")
            return self._resolve_runtime_artifact(context, payload["reference"])
        if capability == "facts:read" and operation == "read-executor-facts":
            from .runtime_hosting import InvocationOwner
            from .runtime_executor_contracts import ExecutorReference
            require(type(payload) is dict and set(payload) == {"port", "producer", "executor_ref", "fact_ids"},
                    "runtime_fact_reference_invalid", "Fact resolution fields differ")
            owner = InvocationOwner.from_dict(payload["producer"]).to_dict()
            reference = ExecutorReference.from_dict(payload["executor_ref"]).to_dict()
            origin = self._resolve_runtime_input_detail(context, payload["port"])
            require(origin["producer"] == owner, "runtime_fact_reference_denied",
                    "Facts must belong to the exact accepted input producer", 403)
            with self._lock, closing(self._store()) as store:
                return RuntimeFactStore(store).executor_facts(owner, reference, payload["fact_ids"])
        return super()._host_call(context, capability, operation, payload)
