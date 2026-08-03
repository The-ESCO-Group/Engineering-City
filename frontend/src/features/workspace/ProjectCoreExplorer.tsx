import {
  AlertTriangle,
  BookOpenCheck,
  Boxes,
  Braces,
  Cable,
  CircleHelp,
  Download,
  ExternalLink,
  Filter,
  GitBranch,
  Link2,
  ListTree,
  Search,
  SearchX,
  ShieldCheck,
  Tags
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import type {
  WorkspaceCapability,
  WorkspaceDevice,
  WorkspaceProgram,
  WorkspaceRequirement,
  WorkspaceTag
} from "./types";

export type ProjectCoreView = "requirements" | "devices" | "tags" | "plc";

export interface ProjectCoreExplorerProps {
  view: ProjectCoreView;
  capability: WorkspaceCapability;
  requirements: WorkspaceRequirement[];
  devices: WorkspaceDevice[];
  tags: WorkspaceTag[];
  plcPrograms: WorkspaceProgram[];
  onExport?: (view: ProjectCoreView) => Promise<void>;
}

const VIEW_META: Record<ProjectCoreView, { eyebrow: string; title: string; description: string }> = {
  requirements: {
    eyebrow: "CANONICAL PROJECT CORE",
    title: "Requirements & coverage",
    description: "Source trace, acceptance criteria, implementation coverage, tests, and unresolved questions."
  },
  devices: {
    eyebrow: "CANONICAL PROJECT CORE",
    title: "Devices & I/O",
    description: "Device identity, channel assignments, fail states, cross-references, and discipline coverage."
  },
  tags: {
    eyebrow: "CANONICAL PROJECT CORE",
    title: "Tag register",
    description: "Scope, type, alias, validation, references, and revision provenance."
  },
  plc: {
    eyebrow: "CANONICAL PROJECT CORE",
    title: "PLC structure & logic",
    description: "Controller hierarchy, routines, structured instructions, warnings, requirements, and evidence links."
  }
};

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function ProjectCoreExplorer(props: ProjectCoreExplorerProps) {
  const meta = VIEW_META[props.view];
  const exportView = async () => props.onExport?.(props.view);
  return (
    <section className="eng-core-workspace" aria-labelledby={`core-title-${props.view}`}>
      <header className="eng-workspace-header">
        <div><span className="eng-eyebrow">{meta.eyebrow}</span><h2 id={`core-title-${props.view}`}>{meta.title}</h2><p>{meta.description}</p></div>
        <button
          type="button"
          className="eng-secondary-action"
          disabled={!props.onExport}
          onClick={() => void exportView()}
          title={!props.onExport ? "No generated export endpoint is available for this view." : undefined}
        ><Download aria-hidden="true" /> Export generated view</button>
      </header>
      {props.capability.level !== "available" && <div className="eng-capability-notice"><CircleHelp aria-hidden="true" /><p><b>{humanize(props.capability.level)}</b> · {props.capability.reason}</p></div>}
      {props.view === "requirements" && <RequirementsView requirements={props.requirements} capability={props.capability} />}
      {props.view === "devices" && <DevicesView devices={props.devices} capability={props.capability} />}
      {props.view === "tags" && <TagsView tags={props.tags} capability={props.capability} />}
      {props.view === "plc" && <PlcView programs={props.plcPrograms} capability={props.capability} />}
    </section>
  );
}

function SearchToolbar({
  value,
  onChange,
  placeholder,
  count,
  total,
  filter
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  count: number;
  total: number;
  filter?: ReactNode;
}) {
  return (
    <div className="eng-toolbar">
      <label className="eng-search"><Search aria-hidden="true" /><span className="sr-only">Search Project Core</span><input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} /></label>
      {filter}
      <span className="eng-result-count">{count} of {total}</span>
    </div>
  );
}

function CoreEmpty({ capability, label }: { capability: WorkspaceCapability; label: string }) {
  return <div className="eng-large-empty"><SearchX aria-hidden="true" /><h3>No {label} published</h3><p>{capability.reason}</p></div>;
}

function Coverage({ value, label }: { value: number | null; label: string }) {
  if (value === null) return <span className="eng-coverage unknown" title={`${label} was not published`}>Not published</span>;
  return <span className={`eng-coverage ${value === 100 ? "complete" : value > 0 ? "partial" : "none"}`}>{value}%</span>;
}

function RequirementsView({ requirements, capability }: { requirements: WorkspaceRequirement[]; capability: WorkspaceCapability }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [selectedId, setSelectedId] = useState<string | null>(requirements[0]?.id ?? null);
  const statuses = useMemo(() => [...new Set(requirements.map((item) => item.status))].sort(), [requirements]);
  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return requirements.filter((requirement) => {
      const haystack = `${requirement.reference} ${requirement.title} ${requirement.text} ${requirement.source}`.toLowerCase();
      return (!normalized || haystack.includes(normalized)) && (status === "all" || requirement.status === status);
    });
  }, [query, requirements, status]);
  const selected = visible.find((item) => item.id === selectedId) ?? visible[0] ?? null;
  return (
    <>
      <SearchToolbar value={query} onChange={setQuery} placeholder="Search reference, title, text, or source…" count={visible.length} total={requirements.length} filter={
        <label className="eng-select"><Filter aria-hidden="true" /><span className="sr-only">Filter requirement status</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All statuses</option>{statuses.map((item) => <option key={item} value={item}>{humanize(item)}</option>)}</select></label>
      } />
      {requirements.length === 0 ? <CoreEmpty capability={capability} label="requirements" /> : (
        <div className="eng-split-records">
          <div className="eng-table-scroll"><table className="eng-data-table"><thead><tr><th>Requirement</th><th>Source</th><th>Status</th><th>Implementation</th><th>Tests</th><th>Questions</th></tr></thead><tbody>{visible.map((requirement) => (
            <tr key={requirement.id} className={selected?.id === requirement.id ? "selected" : ""} id={`requirement-${requirement.id}`}>
              <td><button type="button" className="eng-cell-button" onClick={() => setSelectedId(requirement.id)}><b>{requirement.reference} · {requirement.title}</b><small>{humanize(requirement.kind)} · {humanize(requirement.priority)}{requirement.inferred ? " · inferred proposal" : ""}</small></button></td>
              <td>{requirement.source}</td>
              <td><span className={`eng-status ${requirement.status}`}>{humanize(requirement.status)}</span></td>
              <td><Coverage value={requirement.implementationCoverage} label="Implementation coverage" /></td>
              <td><Coverage value={requirement.testCoverage} label="Test coverage" /></td>
              <td>{requirement.openQuestions.length}</td>
            </tr>
          ))}</tbody></table></div>
          {selected && <RequirementInspector requirement={selected} />}
        </div>
      )}
    </>
  );
}

function RequirementInspector({ requirement }: { requirement: WorkspaceRequirement }) {
  return (
    <aside className="eng-record-inspector">
      <div className="eng-record-icon"><BookOpenCheck aria-hidden="true" /></div>
      <span className={`eng-status ${requirement.status}`}>{humanize(requirement.status)}</span>
      <h3>{requirement.reference}</h3>
      <h4>{requirement.title}</h4>
      <p>{requirement.text}</p>
      <dl className="eng-definition-list compact">
        <div><dt>Source</dt><dd>{requirement.source}</dd></div>
        <div><dt>Kind / priority</dt><dd>{humanize(requirement.kind)} · {humanize(requirement.priority)}</dd></div>
        <div><dt>Confidence</dt><dd>{requirement.confidence === null ? "Not published" : `${Math.round(requirement.confidence * 100)}%`}</dd></div>
        <div><dt>Provenance</dt><dd>{requirement.inferred ? "Inferred proposal" : "Source-authored"}</dd></div>
      </dl>
      <RecordList title={`Acceptance criteria (${requirement.acceptanceCriteriaCount})`} items={requirement.acceptanceCriteria} empty="Criteria content is not published in the current event summary." />
      <RecordList title="Open questions" items={requirement.openQuestions} empty="No open question is linked." />
      <a className="eng-deep-link" href={`#requirement-${requirement.id}`}><Link2 aria-hidden="true" /> Copyable deep link target</a>
    </aside>
  );
}

function DevicesView({ devices, capability }: { devices: WorkspaceDevice[]; capability: WorkspaceCapability }) {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(devices[0]?.id ?? null);
  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return devices.filter((device) => `${device.deviceId} ${device.area} ${device.unit} ${device.type} ${device.vendorModel}`.toLowerCase().includes(normalized));
  }, [devices, query]);
  const selected = visible.find((item) => item.id === selectedId) ?? visible[0] ?? null;
  return (
    <>
      <SearchToolbar value={query} onChange={setQuery} placeholder="Search device, area, unit, type, or model…" count={visible.length} total={devices.length} />
      {devices.length === 0 ? <CoreEmpty capability={capability} label="devices or I/O assignments" /> : (
        <div className="eng-split-records">
          <div className="eng-table-scroll"><table className="eng-data-table"><thead><tr><th>Device</th><th>Area / unit</th><th>I/O assignment</th><th>Fail state</th><th>Coverage</th><th>Validation</th></tr></thead><tbody>{visible.map((device) => (
            <tr key={device.id} className={selected?.id === device.id ? "selected" : ""} id={`device-${device.id}`}>
              <td><button type="button" className="eng-cell-button" onClick={() => setSelectedId(device.id)}><b>{device.deviceId}</b><small>{device.type} · {device.vendorModel}</small></button></td>
              <td>{device.area}<small className="eng-cell-note">{device.unit}</small></td>
              <td>{device.ioAssignments[0] ?? "Not assigned"}<small className="eng-cell-note">{device.networkNode}</small></td>
              <td>{device.failState || "Not published"}</td>
              <td><span className="eng-coverage-set">H {device.coverage.hmi} · A {device.coverage.alarms} · T {device.coverage.tests}</span></td>
              <td><span className={`eng-status ${device.validation.toLowerCase().includes("conflict") ? "conflict" : "valid"}`}>{device.validation}</span></td>
            </tr>
          ))}</tbody></table></div>
          {selected && <DeviceInspector device={selected} />}
        </div>
      )}
    </>
  );
}

function DeviceInspector({ device }: { device: WorkspaceDevice }) {
  return (
    <aside className="eng-record-inspector">
      <div className="eng-record-icon orange"><Cable aria-hidden="true" /></div>
      <span className="eng-eyebrow">{device.area} · {device.unit}</span>
      <h3>{device.deviceId}</h3><h4>{device.type}</h4><p>{device.vendorModel}</p>
      <RecordList title="I/O assignments" items={device.ioAssignments} empty="No assignment published." />
      <dl className="eng-definition-list compact"><div><dt>Network node</dt><dd>{device.networkNode || "Not published"}</dd></div><div><dt>Fail state</dt><dd>{device.failState || "Not published"}</dd></div><div><dt>Validation</dt><dd>{device.validation}</dd></div></dl>
      <RecordList title="Linked tags" items={device.linkedTags} empty="No linked tags published." />
      <RecordList title="Linked logic" items={device.linkedLogic} empty="No linked logic published." />
      <a className="eng-deep-link" href={`#device-${device.id}`}><Link2 aria-hidden="true" /> Device deep link</a>
    </aside>
  );
}

function TagsView({ tags, capability }: { tags: WorkspaceTag[]; capability: WorkspaceCapability }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [selectedId, setSelectedId] = useState<string | null>(tags[0]?.id ?? null);
  const statuses = useMemo(() => [...new Set(tags.map((tag) => tag.status))].sort(), [tags]);
  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return tags.filter((tag) => {
      const haystack = `${tag.name} ${tag.scope} ${tag.dataType} ${tag.role} ${tag.description} ${tag.alias} ${tag.device}`.toLowerCase();
      return haystack.includes(normalized) && (status === "all" || tag.status === status);
    });
  }, [query, status, tags]);
  const selected = visible.find((tag) => tag.id === selectedId) ?? visible[0] ?? null;
  return (
    <>
      <SearchToolbar value={query} onChange={setQuery} placeholder="Search tag, alias, device, type, or description…" count={visible.length} total={tags.length} filter={
        <label className="eng-select"><Filter aria-hidden="true" /><span className="sr-only">Filter tag status</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All statuses</option>{statuses.map((item) => <option key={item}>{item}</option>)}</select></label>
      } />
      {tags.length === 0 ? <CoreEmpty capability={capability} label="tags" /> : (
        <div className="eng-split-records">
          <div className="eng-table-scroll"><table className="eng-data-table"><thead><tr><th>Tag</th><th>Scope / type</th><th>Role</th><th>Alias / device</th><th>Status</th><th>Revision</th></tr></thead><tbody>{visible.map((tag) => (
            <tr key={tag.id} className={selected?.id === tag.id ? "selected" : ""} id={`tag-${tag.id}`}>
              <td><button type="button" className="eng-cell-button" onClick={() => setSelectedId(tag.id)}><b className="eng-mono-name">{tag.name}</b><small>{tag.description || "No description"}</small></button></td>
              <td>{tag.scope}<small className="eng-cell-note">{tag.dataType}{tag.units ? ` · ${tag.units}` : ""}</small></td>
              <td>{tag.role}</td><td>{tag.alias || "Not an alias"}<small className="eng-cell-note">{tag.device || "No device link"}</small></td>
              <td><span className={`eng-status ${tag.status.toLowerCase().replaceAll(" ", "_")}`}>{tag.status}</span></td><td>{tag.approvedRevision === null ? "Unapproved" : `r${tag.approvedRevision}`}</td>
            </tr>
          ))}</tbody></table></div>
          {selected && <TagInspector tag={selected} />}
        </div>
      )}
    </>
  );
}

function TagInspector({ tag }: { tag: WorkspaceTag }) {
  return (
    <aside className="eng-record-inspector">
      <div className="eng-record-icon purple"><Tags aria-hidden="true" /></div><span className="eng-eyebrow">{tag.scope} · {tag.dataType}</span><h3 className="eng-mono-name">{tag.name}</h3><p>{tag.description || "No description was published."}</p>
      <dl className="eng-definition-list compact"><div><dt>Role</dt><dd>{tag.role}</dd></div><div><dt>Alias</dt><dd><code>{tag.alias || "Not an alias"}</code></dd></div><div><dt>Device</dt><dd>{tag.device || "No device link"}</dd></div><div><dt>Validation</dt><dd>{tag.validation}</dd></div><div><dt>Approved revision</dt><dd>{tag.approvedRevision === null ? "Not approved" : `Revision ${tag.approvedRevision}`}</dd></div></dl>
      <RecordList title={`Cross-references (${tag.references.length})`} items={tag.references} empty="No references published." />
      <a className="eng-deep-link" href={`#tag-${tag.id}`}><Link2 aria-hidden="true" /> Tag deep link</a>
    </aside>
  );
}

function PlcView({ programs, capability }: { programs: WorkspaceProgram[]; capability: WorkspaceCapability }) {
  const allRungs = useMemo(() => programs.flatMap((program) => program.routines.flatMap((routine) => routine.rungs.map((rung) => ({ program, routine, rung })))), [programs]);
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(allRungs[0]?.rung.id ?? null);
  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return allRungs.filter(({ program, routine, rung }) => `${program.controller} ${program.name} ${routine.name} ${rung.number} ${rung.comment} ${rung.instructions.map((item) => `${item.mnemonic} ${item.operands.join(" ")}`).join(" ")}`.toLowerCase().includes(normalized));
  }, [allRungs, query]);
  const selected = visible.find(({ rung }) => rung.id === selectedId) ?? visible[0] ?? null;
  return (
    <>
      <SearchToolbar value={query} onChange={setQuery} placeholder="Search program, routine, rung, instruction, or operand…" count={visible.length} total={allRungs.length} />
      {programs.length === 0 ? <CoreEmpty capability={capability} label="PLC structure" /> : (
        <div className="eng-plc-layout">
          <nav className="eng-logic-tree" aria-label="PLC logic tree">
            {programs.map((program) => <section key={program.id}><h3><Boxes aria-hidden="true" /><span>{program.controller}<small>{program.task}</small></span></h3><div className="eng-tree-program"><b><GitBranch aria-hidden="true" />{program.name}</b>{program.routines.map((routine) => <div key={routine.id}><strong><ListTree aria-hidden="true" />{routine.name}<small>{routine.type}</small></strong>{routine.rungs.map((rung) => <button type="button" key={rung.id} className={selected?.rung.id === rung.id ? "selected" : ""} onClick={() => setSelectedId(rung.id)}>Rung {rung.number}<span>{rung.warnings.length ? <AlertTriangle aria-label={`${rung.warnings.length} warning`} /> : <ShieldCheck aria-label="No published warnings" />}</span></button>)}</div>)}</div></section>)}
          </nav>
          <section className="eng-rung-viewer" aria-label="PLC rung viewer">
            {selected && <>
              <header><div><span className="eng-eyebrow">{selected.program.name} / {selected.routine.name}</span><h3>Rung {selected.rung.number}</h3><p>{selected.rung.comment || "No rung comment was published."}</p></div><button type="button" disabled={!selected.program.generatedPreviewAvailable} title={!selected.program.generatedPreviewAvailable ? "No generated L5X artifact is available. This structured view is not an import file." : undefined}><ExternalLink aria-hidden="true" /> L5X preview</button></header>
              <p className="eng-validation-banner"><ShieldCheck aria-hidden="true" />{selected.program.validation}</p>
              <div className="eng-ladder-preview" aria-label={`Structured instructions for rung ${selected.rung.number}`}>
                <i /><div>{selected.rung.instructions.map((instruction, index) => <div className="eng-instruction" key={`${instruction.mnemonic}-${index}`}><span>{instruction.mnemonic}</span><code>{instruction.operands.join(", ")}</code><small>{instruction.description}</small></div>)}</div><i />
              </div>
              {selected.rung.warnings.map((warning) => <p className="eng-alert-block" key={warning}><AlertTriangle aria-hidden="true" />{warning}</p>)}
              <div className="eng-link-columns"><RecordList title="Requirement links" items={selected.rung.requirementLinks} empty="No requirement link published." /><RecordList title="Evidence links" items={selected.rung.evidenceLinks} empty="No evidence link published." /></div>
              <p className="eng-muted-note"><Braces aria-hidden="true" />Template origin: {selected.routine.templateOrigin}</p>
            </>}
          </section>
        </div>
      )}
    </>
  );
}

function RecordList({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  return <div className="eng-record-list"><h5>{title}</h5>{items.length ? <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul> : <p>{empty}</p>}</div>;
}
