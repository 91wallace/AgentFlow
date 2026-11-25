"""CLI presentation only; configuration rules and storage are injected."""

from .agents.roles import ExecutionRoles, ROLE_NAMES, RoleBinding
from .agents.selection import ModelSelectionRequest
from .apl.role_selection import binding_for, validate_roles


def present_inventory(reports, output_stream):
    for report in reports:
        observed = report.availability
        print(f"{observed.adapter_id}: {observed.status}; versão={observed.version or 'unknown'}; "
              f"catálogo={report.model_listing}; autenticação={report.authentication}", file=output_stream)
        print(f"  perfil AgentFlow: {report.configured_provider or '?'}/{report.configured_model or '?'}; "
              "perfil configurado não comprova acesso nem é o padrão nativo do CLI", file=output_stream)


def choose_roles(store, request_id, reports, requested, *, input_stream, output_stream,
                 validate_binding=None):
    saved = store.configuration(request_id)
    if saved is not None:
        validate_roles(saved, reports)
        for role, options in requested.items():
            selected = getattr(saved, role)
            if role == "reviewer" and options.get("agent") == "same":
                if selected != saved.orchestrator or options.get("provider") or options.get("model"):
                    raise ValueError("role_configuration_immutable")
                continue
            if (options.get("agent") is not None and options["agent"] != selected.agent_id
                    or options.get("provider") is not None and options["provider"] != selected.provider
                    or options.get("model") is not None and options["model"] != selected.model):
                raise ValueError("role_configuration_immutable: use a new operation-id")
        return saved
    by_id = {report.descriptor.adapter_id: report for report in reports}
    interactive = input_stream.isatty()
    selected = {}
    drafts = store.draft_selections(request_id)
    for role in ROLE_NAMES:
        options = requested.get(role, {})
        agent = options.get("agent")
        if not agent and (options.get("provider") or options.get("model")):
            raise ValueError(f"{role} model requires explicit role agent")
        if not agent and role in drafts:
            data = dict(drafts[role])
            data["model_selection"] = ModelSelectionRequest(**data["model_selection"]) if data["model_selection"] else None
            selected[role] = RoleBinding(**data)
            temporary = ExecutionRoles(selected[role], selected[role], selected[role])
            validate_roles(temporary, reports)
            continue
        if role == "reviewer" and agent is None and interactive:
            print("Escolha reviewer [same = orquestrador]: ", end="", file=output_stream, flush=True)
            agent = input_stream.readline().strip() or "same"
        if role == "reviewer" and agent in (None, "same") and "orchestrator" in selected:
            if options.get("provider") or options.get("model"):
                raise ValueError("reviewer same uses the orchestrator model")
            # The suggested default is made explicit in the persisted snapshot.
            selected[role] = selected["orchestrator"]
            store.save_draft(request_id, role, selected[role])
            continue
        if not agent and interactive:
            print(f"Escolha {role} ({', '.join(by_id)}): ", end="", file=output_stream, flush=True)
            agent = input_stream.readline().strip()
            if not agent:
                return None
        if not agent:
            return None
        if agent not in by_id:
            raise ValueError(f"role_agent_unregistered:{agent}")
        provider, model = options.get("provider") or "", options.get("model") or ""
        if interactive and not model:
            configured = by_id[agent].configured_model or "desconhecido"
            print(f"Modelo {role}: perfil AgentFlow={configured}; acesso não verificado. "
                  "Enter usa o padrão do CLI; ou informe provider/model (provider com barras: use flags): ",
                  end="", file=output_stream, flush=True)
            text = input_stream.readline().strip()
            if text:
                if "/" not in text:
                    raise ValueError("informe provider/model ou use o padrão nativo")
                provider, model = text.split("/", 1)
        selected[role] = binding_for(by_id[agent], provider=provider, model=model)
        if validate_binding:
            validate_binding(selected[role])
        store.save_draft(request_id, role, selected[role])
    return store.configure(request_id, ExecutionRoles(**selected))
