import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const required = ['json-bigint'];
for (const name of required) {
  try {
    require.resolve(name);
  } catch (error) {
    console.error(`missing production dependency: ${name}`);
    process.exitCode = 1;
  }
}
