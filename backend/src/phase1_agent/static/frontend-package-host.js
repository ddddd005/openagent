"use strict";

((root) => {
  const { exact, clone, strictJson } = root.GraphChat;
  const bounded = value => typeof value === "string" && value.length > 0 && value.length <= 128 && value.trim() === value;
  const freeze = value => {
    if (value && typeof value === "object") { Object.values(value).forEach(freeze); Object.freeze(value); }
    return value;
  };
  const same = (a, b) => {
    if (a === b) return true;
    if (Array.isArray(a)) return Array.isArray(b) && a.length === b.length && a.every((item, i) => same(item, b[i]));
    return a !== null && b !== null && typeof a === "object" && typeof b === "object"
      && Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(key => Object.hasOwn(b, key) && same(a[key], b[key]));
  };
  function require(value, reason) { if (!value) throw new Error(reason); }
  function validateExtension(extension) {
    require(exact(extension, ["schema_version", "host_protocol_version", "extension_id", "kind", "entrypoint",
      "component_id", "component_version", "package_id", "package_version", "binding"])
      && extension.schema_version === 2 && extension.host_protocol_version === 1 && extension.kind === "consumer"
      && ["extension_id", "package_id", "package_version"].every(key => bounded(extension[key]))
      && typeof extension.entrypoint === "string" && extension.entrypoint.length <= 512
      && extension.entrypoint.trim() === extension.entrypoint
      && /^[A-Za-z_][A-Za-z0-9_.:-]*$/.test(extension.entrypoint)
      && extension.component_id === null && extension.component_version === null
      && exact(extension.binding, ["surface", "slot", "target"]) && extension.binding.surface === "consumer"
      && extension.binding.slot === "public-output"
      && exact(extension.binding.target, ["scope", "type_id", "schema_version"])
      && extension.binding.target.scope === "content" && bounded(extension.binding.target.type_id)
      && Number.isSafeInteger(extension.binding.target.schema_version) && extension.binding.target.schema_version > 0,
    "消费界面扩展契约无效");
    return extension;
  }
  function validateSelection(selection, output) {
    require(Array.isArray(selection.package_lock) && selection.package_lock.length <= 256
      && selection.package_lock.every(item => exact(item, ["package_id", "version"]) && bounded(item.package_id) && bounded(item.version))
      && new Set(selection.package_lock.map(item => item.package_id)).size === selection.package_lock.length
      && Array.isArray(selection.frontend_extensions) && selection.frontend_extensions.length <= 4096,
    "消费界面包选择无效");
    const ids = new Set(), bindings = new Set();
    for (const item of selection.frontend_extensions) {
      validateExtension(item);
      const binding = JSON.stringify([item.binding.surface, item.binding.slot, item.binding.target.type_id, item.binding.target.schema_version]);
      require(!ids.has(item.extension_id) && !bindings.has(binding)
        && selection.package_lock.some(pkg => pkg.package_id === item.package_id && pkg.version === item.package_version)
        && item.binding.target.type_id === output.data_type
        && item.binding.target.schema_version === (output.data_schema_version ?? output.payload?.schema_version),
      "消费界面扩展重复、缺包或绑定不匹配");
      ids.add(item.extension_id); bindings.add(binding);
    }
    return selection;
  }
  class ConsumerFrontendHost {
    constructor(client) {
      this.client = client; this.implementations = new Map(); this.mounts = new Set();
      this.selectionKey = null; this.selectionSequence = 0; this.acceptedSequence = 0;
    }
    register(declaration, mount) {
      const checked = freeze(clone(validateExtension(declaration)));
      require(typeof mount === "function" && !this.implementations.has(checked.entrypoint), "本地界面实现重复或无效");
      this.implementations.set(checked.entrypoint, { declaration: checked, mount });
    }
    invalidate() {
      for (const mount of [...this.mounts]) mount.dispose();
    }
    dispose() { this.invalidate(); this.implementations.clear(); }
    attach(output, container) {
      const fixed = freeze(clone(output)), initialContext = clone(this.client.informationContext());
      const sequence = ++this.selectionSequence;
      let active = true, disposeRenderer = null;
      const current = () => active && same(initialContext, this.client.informationContext())
        && [...(this.client.consumer?.outputs ?? []), ...(this.client.publicHistory ?? [])].some(item => same(item, fixed));
      const handle = {
        dispose: () => {
          if (!active) return;
          active = false; this.mounts.delete(handle);
          if (disposeRenderer) disposeRenderer();
        },
        ready: null,
      };
      this.mounts.add(handle);
      handle.ready = (async () => {
        try {
          const selected = await this.client.readFrontendExtensions(fixed);
          if (!current() || !selected) return "obsolete";
          validateSelection(selected, fixed);
          const selectionKey = JSON.stringify([...selected.package_lock].sort((a, b) => a.package_id.localeCompare(b.package_id)));
          if (sequence < this.acceptedSequence && selectionKey !== this.selectionKey) {
            handle.dispose(); return "obsolete";
          }
          this.acceptedSequence = Math.max(sequence, this.acceptedSequence);
          if (this.selectionKey !== null && selectionKey !== this.selectionKey)
            for (const other of [...this.mounts]) if (other !== handle) other.dispose();
          this.selectionKey = selectionKey;
          if (!selected.frontend_extensions.length) return "unregistered";
          const declaration = selected.frontend_extensions[0], local = this.implementations.get(declaration.entrypoint);
          if (!local || !same(local.declaration, declaration)) return "implementation-unavailable";
          const sdk = Object.freeze({
            current,
            readEventBindings: async () => {
              if (!current()) return null;
              try { const value = await this.client.readEventBindings(); return current() ? value : null; }
              catch (error) { if (!current()) return null; throw error; }
            },
            submitEvent: async (binding, payload) => {
              require(current(), "前端事件提交依据已过期");
              require(strictJson(binding) && payload !== null && typeof payload === "object" && !Array.isArray(payload)
                && strictJson(payload), "事件提交须为严格 JSON 对象");
              return this.client.submitEvent(clone(binding), clone(payload));
            },
            readEvent: async chainId => {
              if (!current()) return null;
              try { const value = await this.client.readEvent(chainId); return current() ? value : null; }
              catch (error) { if (!current()) return null; throw error; }
            },
            readArtifact: async reference => {
              if (!current()) return null;
              try {
                const value = await this.client.readOutputArtifact(fixed, clone(reference));
                return current() ? value : null;
              } catch (error) { if (!current()) return null; throw error; }
            },
          });
          const dispose = local.mount({ output: fixed, container, sdk });
          require(typeof dispose === "function", "界面实现必须返回卸载函数");
          disposeRenderer = dispose;
          if (!current()) { handle.dispose(); return "obsolete"; }
          return "mounted";
        } catch (error) {
          if (!current()) return "obsolete";
          handle.dispose(); container.replaceChildren(); throw error;
        }
      })();
      return handle;
    }
  }
  root.WorkflowFrontendHost = { ConsumerFrontendHost, validateExtension, validateSelection };
})(globalThis);
