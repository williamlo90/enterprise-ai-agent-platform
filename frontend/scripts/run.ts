import process from 'node:process';
import { spawn } from 'node:child_process';
import { assertRuntime, createLaunchPlan, frontendRoot } from './commands.ts';

try {
  assertRuntime(process.versions.node, process.env.npm_config_user_agent);
  const [command = '', ...args] = process.argv.slice(2);
  const plan = createLaunchPlan(command, args, process.env);
  const child = spawn(process.execPath, [plan.entry, ...plan.args], {
    cwd: frontendRoot,
    shell: false,
    stdio: 'inherit',
    env: { ...process.env, ...plan.environment, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1' },
  });
  const interrupt = () => { child.kill('SIGINT'); };
  const terminate = () => { child.kill('SIGTERM'); };
  process.once('SIGINT', interrupt);
  process.once('SIGTERM', terminate);
  child.on('error', () => {
    process.stderr.write('Frontend tool could not start. Check the pinned install.\n');
    process.exitCode = 1;
  });
  child.on('exit', (code) => {
    process.removeListener('SIGINT', interrupt);
    process.removeListener('SIGTERM', terminate);
    process.exitCode = code ?? 1;
  });
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : 'Frontend command failed.'}\n`);
  process.exitCode = 1;
}
