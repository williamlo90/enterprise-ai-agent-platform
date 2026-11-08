# Resource safety policy

Shared project limits apply across chats and agents: at most two test workers in total, test
files executed sequentially, and one validation job at a time. Frontend test configuration
enforces bounded workers and disables file parallelism; browser checks run serially.

Start with targeted checks. Reuse applicable results and expand only for changed behavior or
a concrete unresolved failure. Assign a sole executor if a full regression is required; other
reviewers inspect source and evidence rather than automatically repeating the run.

Documentation changes use read-back, links, and static diffs. Do not start full suites, builds,
database probes, model inference, or cloud workloads for repository presentation changes.
Stop only temporary processes created for the task; preserve user services and machine settings.

Model-quality evaluation and deployment acceptance are owner-operated. Infrastructure definitions,
example configuration, and available scripts do not automatically authorize provisioning, paid
requests, migration, or deployment. GitHub workflows are manually invoked.
