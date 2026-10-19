"""Application wiring for public package capabilities and artifact authority."""

from copy import deepcopy

from .graph_records import require
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

    def _execution_read(self, chain_id):
        """Bind a live host read to the invocation's retained run basis."""
        basis = self._require_run_basis(chain_id)
        return self._data_management.read_execution(registry=basis.registry)

    def _execution_read_current(self, chain_id):
        """Read-only fallback for explicit diagnostics, cleanup and notifications.

        It must not be used to authorize an active host call or a service
        invocation: those require the retained basis above.
        """
        basis = self._execution_registries.get(chain_id)
        registry = basis.registry if basis is not None else self.registry
        return self._data_management.read_execution(registry=registry)

    def _resolve_runtime_input(self, context, port):
        return self._resolve_runtime_input_detail(context, port)["value"]

    def _runtime_input_detail(self, data, context, port):
        refs = context.input_artifact_refs(port)
        require(len(refs) == 1, "runtime_input_unbound",
                "Capability requires one exact accepted input", 409)
        run = data.records.node_run(context.node_run_id)
        require(run["input_refs"].get(port) == refs, "runtime_input_unbound",
                "Input differs from the accepted invocation binding", 409)
        output = data.records.output(refs[0]["output_id"])
        producer = data.records.node_run(output["run_id"])
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

    def _resolve_runtime_input_detail(self, context, port):
        with self._lock, self._execution_read(context.chain_run_id) as data:
            return self._runtime_input_detail(data, context, port)

    def _describe_runtime_input_origin(self, context, port):
        """Describe one exact bound input without granting a selectable run lookup."""
        with self._lock, self._execution_read_current(context.chain_run_id) as data:
            consumer = data.records.node_run(context.node_run_id)
            session = data.records.session(context.workflow_session_id)
            chain = data.records.chain(context.chain_run_id)
            require(consumer["workflow_session_id"] == context.workflow_session_id
                    and consumer["chain_run_id"] == context.chain_run_id
                    and consumer["node_binding_id"] == context.node_binding_id
                    and consumer["status"] == "running"
                    and chain["status"] == "running"
                    and session["active_chain_run_id"] == context.chain_run_id,
                    "runtime_origin_owner_mismatch", "Input origin caller is no longer this running invocation", 409)
            detail = self._runtime_input_detail(data, context, port)
            output = data.records.output(detail["output_id"])
            producer = data.records.node_run(output["run_id"])
            document = self._document_for_runtime_output(data, output)
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
        with self._lock:
            self._check_open()
            return self._data_management.accept_fact(owner, stream_id, fact)

    def _accept_service_fact(self, context, fact, stream_id):
        owner = {"workflow_session_id": context.workflow_session_id,
                 "chain_run_id": context.chain_run_id,
                 "node_binding_id": context.node_binding_id, "node_run_id": context.node_run_id}
        return self._accept_runtime_fact(owner, fact, stream_id)

    @staticmethod
    def _model_authority_record(connection, kind, **identity):
        from .contract_json import loads_strict
        from .graph_records import validate_graph_record
        from .storage import _record_id
        row = connection.execute(
            "SELECT payload FROM records WHERE record_type=? AND record_id=?",
            (kind, _record_id(kind, identity)),
        ).fetchone()
        require(row is not None, "model_binding_unauthorized",
                "Exact model authority record is unavailable", 403)
        return validate_graph_record(kind, loads_strict(row["payload"]))

    @staticmethod
    def _model_authority_declaration(frame, run, *, grants=False):
        node = frame.nodes.get(run["node_binding_id"])
        require(node is not None and node["config"] == run["config"],
                "model_binding_unauthorized",
                "Model authority differs from the frozen runtime declaration", 403)
        declaration = {key: deepcopy(node[key]) for key in (
            "node_binding_id", "component_id", "component_version", "config")}
        if grants:
            definition = frame.definitions[run["node_binding_id"]]
            declaration.update(
                capabilities=list(definition.capabilities),
                service_requirements=deepcopy(list(definition.service_requirements)))
        return declaration

    def _model_binding_authority(self, frame, connection, context, output_id):
        get = self._model_authority_record
        output = get(connection, "workflow_output", output_id=output_id)
        producer = get(connection, "node_run", run_id=output["run_id"])
        consumer = get(connection, "node_run", run_id=context.node_run_id)
        return {
            "session": get(connection, "workflow_session",
                           workflow_session_id=context.workflow_session_id),
            "chain": get(connection, "chain_run", chain_run_id=context.chain_run_id),
            "consumer_run": consumer, "producer_run": producer, "binding_output": output,
            "source_node": self._model_authority_declaration(frame, producer),
            "consumer_node": self._model_authority_declaration(frame, consumer, grants=True),
        }

    def _model_fact_authority(self, frame, connection, context, port):
        get = self._model_authority_record
        consumer = get(connection, "node_run", run_id=context.node_run_id)
        refs = consumer["input_refs"].get(port)
        require(port == "receipts" and type(refs) is list and len(refs) == 1,
                "model_fact_reference_denied",
                "Model facts require one exact accepted receipts input", 403)
        output = get(connection, "workflow_output", output_id=refs[0]["output_id"])
        producer = get(connection, "node_run", run_id=output["run_id"])
        model_refs = producer["input_refs"].get("model")
        require(type(model_refs) is list and len(model_refs) == 1,
                "model_fact_reference_denied",
                "Receipt producer requires its exact accepted model input", 403)
        binding = get(connection, "workflow_output", output_id=model_refs[0]["output_id"])
        source = get(connection, "node_run", run_id=binding["run_id"])
        return {
            "session": get(connection, "workflow_session",
                           workflow_session_id=context.workflow_session_id),
            "chain": get(connection, "chain_run", chain_run_id=context.chain_run_id),
            "consumer_run": consumer, "producer_run": producer, "input_output": output,
            "model_binding_output": binding, "model_source_run": source,
            "source_node": self._model_authority_declaration(frame, producer),
            "model_source_node": self._model_authority_declaration(frame, source),
            "consumer_node": self._model_authority_declaration(frame, consumer, grants=True),
        }

    def _prepare_public_capabilities(self, sid, chain_id, plan, resources, *, registry=None):
        from .host_services import HostServiceEnvironment, HostServiceRun
        registry = self.registry if registry is None else registry
        nodes = {node["node_binding_id"]: node for node in plan.document["nodes"]
                 if node["node_binding_id"] in plan.ordered_node_ids}
        frame = HostServiceRun(registry.services, workflow_session_id=sid, chain_run_id=chain_id,
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
                return self._resolve_runtime_artifact(context, artifact, registry=registry)

            def accept_fact(context, fact, stream_id):
                authorize(context)
                return self._accept_service_fact(context, fact, "service:" + reference.service_id + "@"
                                                 + reference.exact_version + ":" + stream_id)

            def read_resource(resource):
                from .host_sdk import ResourceIdentity
                from .content_configuration import CONTENT_RESOURCE_TYPES
                live()
                identity = ResourceIdentity.from_dict(resource).to_dict()
                require((identity["scope"], identity["type_id"], identity["resource_id"]) in allowed_resources,
                        "graph_resource_dependency_undeclared",
                        "Service resource was not declared in this frozen run", 403)
                if identity["type_id"] in CONTENT_RESOURCE_TYPES:
                    return self.content_configuration.get_global_resource(
                        identity, types=registry.data_types)
                return self.get_global_resource(identity)

            options = {key: value if callable(value) else deepcopy(value)
                       for key, value in self._service_options.get(reference, {}).items()}
            from .model_request_contracts import MODEL_REQUEST_SERVICE_REF
            if reference == MODEL_REQUEST_SERVICE_REF:
                from .model_request_resources import ModelRequestResourceReader
                from .model_request_fact_reader import ModelRequestFactReader
                options["model_resource_reader"] = ModelRequestResourceReader(
                    self.database, types=registry.data_types,
                    load_binding_authority=lambda connection, context, output_id:
                        self._model_binding_authority(frame, connection, context, output_id),
                    source_component=("models.source", "7"))
                options["model_fact_reader"] = ModelRequestFactReader(
                    self.database,
                    load_fact_authority=lambda connection, context, port:
                        self._model_fact_authority(frame, connection, context, port),
                    source_component=("agents.execute", "10"),
                    model_source_component=("models.source", "7"))
            return HostServiceEnvironment(resolve_input, resolve_artifact, accept_fact, read_resource, options)
        frame.prepare(environment, resources)

    def _release_public_capabilities(self, sid, chain_id, *, known_chain=None):
        self._release_tool_bindings(chain_id)
        frame = self._service_runs.get(chain_id)
        if frame is not None and frame.release():
            self._service_runs.pop(chain_id, None)
        host = self._runtime_hosts.get(chain_id)
        if host is not None:
            if known_chain is None:
                # Cleanup may run after the binding was released; it only
                # needs the persisted owner/release coordinates.
                with self._execution_read_current(chain_id) as data:
                    chain = data.records.chain(chain_id)
            else:
                chain = known_chain
                require(chain["chain_run_id"] == chain_id
                        and (sid is None or chain["workflow_session_id"] == sid),
                        "execution_cleanup_owner_mismatch",
                        "Execution cleanup evidence belongs to another owner", 409)
            from .runtime_hosting import InvocationOwner
            for node_id, run_id in zip(chain["ordered_nodes"], chain["node_run_ids"]):
                owner = InvocationOwner(chain["workflow_session_id"], chain_id, node_id, run_id)
                if host.contains(owner):
                    host.release(owner)
            if host.active_handle_count == 0:
                self._runtime_hosts.pop(chain_id, None)
        self._release_information_chain(chain_id, session_id=sid, known_chain=known_chain)

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
        with self._lock:
            host = self._runtime_hosts.get(chain_id)
            if host is None:
                return
            with self._execution_read_current(chain_id) as data:
                chain = data.records.chain(chain_id)
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

    def _artifact_closure(self, data, roots, *, registry=None):
        """Walk registered references; never guess references from business fields."""
        if not hasattr(data, "records"):
            data = self._data_management.execution_read(
                data, registry=registry if registry is not None else self.registry)
        registry = data.registry if registry is None else registry
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
            output = data.records.output(output_id)
            producer = data.records.node_run(output["run_id"])
            require(producer["status"] == "succeeded"
                    and producer["output_refs"].get(output["port_id"]) == output_id,
                    "runtime_artifact_unaccepted", "Artifact has no accepted producer", 409)
            original = self._document_for_runtime_output(data, output)
            node = next(node for node in original["nodes"]
                        if node["node_binding_id"] == output["node_binding_id"])
            entry = registry.get(node["component_id"], node["component_version"])
            require(entry is not None, "graph_missing_node_type",
                    "Artifact's registered implementation is unavailable", 409)
            port = next(port for port in entry.definition.ports(node["config"])[1]
                        if port.port_id == output["port_id"])
            pending.extend(registry.data_types.references(
                port.data_type, port.data_schema_version, output["payload"], scope="content"))
        return seen

    def _document_for_runtime_output(self, data, output):
        chain = data.records.chain(output["chain_run_id"])
        return data.configuration.fixed_document(
            chain["workflow_definition_id"], chain["definition_revision"])

    def _authorized_object_artifacts(self, data, context, *, registry=None):
        registry = data.registry if registry is None else registry
        roots = []
        for read in context.reads:
            if read.get("kind") != "object_read":
                continue
            item = data.state.read_object(
                context.workflow_session_id, read["object_key"], node_id=context.node_binding_id,
                revision_id=read["revision_id"],
            )
            roots.extend(registry.data_types.references(
                item["type_id"], item["schema_version"], item["value"], scope="session"))
        return self._artifact_closure(data, roots, registry=registry)

    def _validate_runtime_references(self, context, references, *, registry=None):
        with self._lock, self._execution_read(context.chain_run_id) as data:
            registry = data.registry if registry is None else registry
            roots = [{"scope": "artifact", "output_id": ref["output_id"]}
                     for refs in context._input_refs.values() for ref in refs]
            allowed = (self._artifact_closure(data, roots, registry=registry)
                       | self._authorized_object_artifacts(data, context, registry=registry))
            return all(ref["scope"] != "artifact" or ref["output_id"] in allowed for ref in references)

    def _resolve_runtime_artifact(self, context, reference, *, registry=None):
        from .host_sdk import validate_reference
        reference = validate_reference(reference)
        require(reference["scope"] == "artifact", "runtime_artifact_reference_invalid",
                "Artifact resolution requires an exact artifact reference")
        with self._lock, self._execution_read(context.chain_run_id) as data:
            registry = data.registry if registry is None else registry
            roots = [{"scope": "artifact", "output_id": ref["output_id"]}
                     for refs in context._input_refs.values() for ref in refs]
            allowed = (self._artifact_closure(data, roots, registry=registry)
                       | self._authorized_object_artifacts(data, context, registry=registry))
            require(reference["output_id"] in allowed, "runtime_artifact_reference_denied",
                    "Artifact is outside the input and authorized object reference closure", 403)
            output = data.records.output(reference["output_id"])
            return {"value": deepcopy(output["payload"]), "output_id": output["output_id"],
                    "producer": {"workflow_session_id": output["workflow_session_id"],
                                 "chain_run_id": output["chain_run_id"],
                                 "node_binding_id": output["node_binding_id"], "node_run_id": output["run_id"]}}

    def _host_call(self, context, capability, operation, payload):
        frame = self._service_runs.get(context.chain_run_id)
        if frame is not None and frame.owns_capability(context, capability):
            with self._lock, self._execution_read(context.chain_run_id) as data:
                run = data.records.node_run(context.node_run_id)
                session = data.records.session(context.workflow_session_id)
                require(run["workflow_session_id"] == context.workflow_session_id
                        and run["chain_run_id"] == context.chain_run_id
                        and run["node_binding_id"] == context.node_binding_id
                        and run["status"] == "running"
                        and session["active_chain_run_id"] == context.chain_run_id,
                        "host_service_owner_mismatch", "Service call owner is no longer running", 409)
            return frame.call(context, capability, operation, payload)
        if capability == "resources:read" and operation == "current-global-resource":
            return self._read_current_resource(context, payload)
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
            registry = self._require_run_basis(context.chain_run_id).registry
            return self._resolve_runtime_artifact(context, payload["reference"], registry=registry)
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
            with self._lock, self._execution_read(context.chain_run_id) as data:
                return data.records.executor_facts(owner, reference, payload["fact_ids"])
        return super()._host_call(context, capability, operation, payload)
