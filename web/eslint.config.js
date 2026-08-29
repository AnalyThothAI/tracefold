// web/eslint.config.js
// Flat config — ESLint 9+
import tsParser from "@typescript-eslint/parser";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import reactPlugin from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import importPlugin from "eslint-plugin-import";
import jsxA11y from "eslint-plugin-jsx-a11y";
import globals from "globals";

const jsxA11yRecommendedRules = jsxA11y.flatConfigs.recommended.rules;
const requiredTestNames = new Set(["it", "test"]);
const requiredTestImportSources = new Set(["vitest", "@playwright/test", "@tests/e2e/fixtures"]);
const testModuleExtensions = "{js,jsx,mjs,cjs,ts,tsx,mts,cts}";
const allTestModules = `tests/**/*.${testModuleExtensions}`;

function staticMemberName(node) {
  if (!node.computed && node.property.type === "Identifier") {
    return node.property.name;
  }
  if (
    node.computed &&
    node.property.type === "Literal" &&
    typeof node.property.value === "string"
  ) {
    return node.property.value;
  }
  return undefined;
}

function memberPath(node) {
  const path = [];
  let current = node;
  while (current.type === "MemberExpression") {
    path.unshift(staticMemberName(current));
    current = current.object;
  }
  return path;
}

function memberRootName(node) {
  let current = node;
  while (current.type === "MemberExpression") {
    current = current.object;
  }
  return current.type === "Identifier" ? current.name : undefined;
}

function isRequiredTestBuilder(callee) {
  return (
    callee.type === "MemberExpression" &&
    requiredTestNames.has(memberRootName(callee)) &&
    ["each", "for"].includes(staticMemberName(callee))
  );
}

function isAllowedRequiredTestMemberPath(path) {
  if (path[0] === "describe") {
    return true;
  }
  if (
    path.length === 1 &&
    [
      "afterAll",
      "afterEach",
      "beforeAll",
      "beforeEach",
      "concurrent",
      "sequential",
      "setTimeout",
      "skip",
      "slow",
      "step",
      "use",
    ].includes(path[0])
  ) {
    return true;
  }
  return (
    ["each", "for"].includes(path[path.length - 1]) &&
    (path.length === 1 || (path.length === 2 && ["concurrent", "sequential"].includes(path[0])))
  );
}

function isRequiredTestInvocation(callee) {
  if (callee.type === "Identifier") {
    return requiredTestNames.has(callee.name);
  }
  if (callee.type === "CallExpression") {
    return isRequiredTestBuilder(callee.callee);
  }
  if (callee.type === "TaggedTemplateExpression") {
    return isRequiredTestBuilder(callee.tag);
  }
  if (callee.type !== "MemberExpression" || !requiredTestNames.has(memberRootName(callee))) {
    return false;
  }
  const path = memberPath(callee);
  return path.length === 1 && ["concurrent", "sequential"].includes(path[0]);
}

function isPlaywrightFixtureFactoryCall(node) {
  return (
    node?.type === "CallExpression" &&
    node.callee.type === "MemberExpression" &&
    memberRootName(node.callee) === "base" &&
    memberPath(node.callee).length === 1 &&
    staticMemberName(node.callee) === "extend"
  );
}

function isExportedPlaywrightFixture(node) {
  return (
    node?.type === "VariableDeclarator" &&
    node.id.type === "Identifier" &&
    node.id.name === "test" &&
    isPlaywrightFixtureFactoryCall(node.init) &&
    node.parent?.type === "VariableDeclaration" &&
    node.parent.parent?.type === "ExportNamedDeclaration"
  );
}

const requiredTestPolicyPlugin = {
  rules: {
    "fixed-declaration": {
      meta: {
        type: "problem",
        docs: {
          description: "Require fixed-shape Vitest and Playwright test declarations.",
        },
        schema: [],
        messages: {
          derivedBinding:
            "Required tests cannot derive or extend a test/it binding; invoke the imported binding directly.",
          dynamicImport:
            "Required test runners must use static named imports; namespace and dynamic runner imports cannot authorize CI.",
          dynamicMember:
            "Required test modifiers must be statically named; computed test/it members cannot authorize CI.",
          fixedShape:
            "Required tests take exactly two arguments: the case name and its callback. Options-form tests cannot authorize CI.",
          indirectBinding:
            "Required test/it bindings cannot be aliased, copied, destructured, passed, or returned; invoke them directly.",
        },
      },
      create(context) {
        const namespaceNames = new Set();
        return {
          ImportDeclaration(node) {
            const source = node.source.value;
            for (const specifier of node.specifiers) {
              if (specifier.type === "ImportNamespaceSpecifier") {
                namespaceNames.add(specifier.local.name);
                if (requiredTestImportSources.has(source)) {
                  context.report({ node: specifier, messageId: "dynamicImport" });
                }
              }
              if (specifier.type === "ImportSpecifier") {
                const imported =
                  specifier.imported.type === "Identifier"
                    ? specifier.imported.name
                    : specifier.imported.value;
                if (requiredTestNames.has(imported) && specifier.local.name !== imported) {
                  context.report({ node: specifier, messageId: "indirectBinding" });
                }
              }
            }
          },
          ImportExpression(node) {
            context.report({ node, messageId: "dynamicImport" });
          },
          ExportAllDeclaration(node) {
            context.report({ node, messageId: "dynamicImport" });
          },
          ExportNamedDeclaration(node) {
            if (node.source && requiredTestImportSources.has(node.source.value)) {
              context.report({ node, messageId: "indirectBinding" });
              return;
            }
            for (const specifier of node.specifiers) {
              if (specifier.type === "ExportNamespaceSpecifier") {
                context.report({ node: specifier, messageId: "dynamicImport" });
                continue;
              }
              const local =
                specifier.local.type === "Identifier"
                  ? specifier.local.name
                  : specifier.local.value;
              const exported =
                specifier.exported.type === "Identifier"
                  ? specifier.exported.name
                  : specifier.exported.value;
              if (requiredTestNames.has(local) || requiredTestNames.has(exported)) {
                context.report({ node: specifier, messageId: "indirectBinding" });
              }
            }
          },
          Identifier(node) {
            if (!requiredTestNames.has(node.name)) {
              return;
            }
            const parent = node.parent;
            if (
              parent?.type === "ImportSpecifier" ||
              (parent?.type === "CallExpression" && parent.callee === node) ||
              (parent?.type === "MemberExpression" &&
                (parent.object === node || (!parent.computed && parent.property === node))) ||
              (parent?.type === "Property" &&
                !parent.computed &&
                !parent.shorthand &&
                parent.key === node)
            ) {
              return;
            }
            context.report({ node, messageId: "indirectBinding" });
          },
          MemberExpression(node) {
            const root = memberRootName(node);
            const path = memberPath(node);
            if (namespaceNames.has(root) && path.some((name) => requiredTestNames.has(name))) {
              context.report({ node, messageId: "dynamicImport" });
            }
            if (!requiredTestNames.has(root)) {
              return;
            }
            if (path.some((name) => name === undefined)) {
              context.report({ node, messageId: "dynamicMember" });
            }
            if (!isAllowedRequiredTestMemberPath(path)) {
              context.report({ node, messageId: "derivedBinding" });
            }
            if (node.parent?.type === "MemberExpression" && node.parent.object === node) {
              return;
            }
            const isDirectUse =
              (node.parent?.type === "CallExpression" && node.parent.callee === node) ||
              (node.parent?.type === "TaggedTemplateExpression" && node.parent.tag === node);
            if (!isDirectUse) {
              context.report({ node, messageId: "indirectBinding" });
            }
          },
          CallExpression(node) {
            if (isRequiredTestInvocation(node.callee) && node.arguments.length !== 2) {
              context.report({ node, messageId: "fixedShape" });
            }
            if (
              node.callee.type === "MemberExpression" &&
              requiredTestNames.has(staticMemberName(node.callee)) &&
              node.arguments.length >= 3
            ) {
              context.report({ node, messageId: "fixedShape" });
            }
            if (isRequiredTestBuilder(node.callee)) {
              const parent = node.parent;
              if (!(parent?.type === "CallExpression" && parent.callee === node)) {
                context.report({ node, messageId: "indirectBinding" });
              }
            }
            if (node.callee.type === "Identifier" && node.callee.name === "require") {
              context.report({ node, messageId: "dynamicImport" });
            }
          },
          TaggedTemplateExpression(node) {
            if (isRequiredTestBuilder(node.tag)) {
              const parent = node.parent;
              if (!(parent?.type === "CallExpression" && parent.callee === node)) {
                context.report({ node, messageId: "indirectBinding" });
              }
            }
          },
        };
      },
    },
    "playwright-fixture-factory": {
      meta: {
        type: "problem",
        docs: {
          description: "Constrain the one shared Playwright fixture factory.",
        },
        schema: [],
        messages: {
          factoryOnly:
            "The shared Playwright fixture may only export `test = base.extend(...)`; it cannot register or alias test cases.",
        },
      },
      create(context) {
        return {
          ImportDeclaration(node) {
            for (const specifier of node.specifiers) {
              if (specifier.type === "ImportNamespaceSpecifier") {
                context.report({ node: specifier, messageId: "factoryOnly" });
              }
              if (specifier.type === "ImportSpecifier") {
                const imported =
                  specifier.imported.type === "Identifier"
                    ? specifier.imported.name
                    : specifier.imported.value;
                if (
                  imported === "test" &&
                  !(node.source.value === "@playwright/test" && specifier.local.name === "base")
                ) {
                  context.report({ node: specifier, messageId: "factoryOnly" });
                }
              }
            }
          },
          ImportExpression(node) {
            context.report({ node, messageId: "factoryOnly" });
          },
          ExportAllDeclaration(node) {
            context.report({ node, messageId: "factoryOnly" });
          },
          Identifier(node) {
            const parent = node.parent;
            if (node.name === "base") {
              if (
                parent?.type === "ImportSpecifier" ||
                (parent?.type === "MemberExpression" && parent.object === node)
              ) {
                return;
              }
              context.report({ node, messageId: "factoryOnly" });
            }
            if (node.name === "test") {
              if (
                parent?.type === "ImportSpecifier" ||
                (parent?.type === "MemberExpression" &&
                  !parent.computed &&
                  parent.property === node) ||
                (parent?.type === "Property" &&
                  !parent.computed &&
                  !parent.shorthand &&
                  parent.key === node) ||
                (parent?.type === "VariableDeclarator" &&
                  parent.id === node &&
                  isExportedPlaywrightFixture(parent))
              ) {
                return;
              }
              context.report({ node, messageId: "factoryOnly" });
            }
          },
          MemberExpression(node) {
            if (memberRootName(node) !== "base") {
              return;
            }
            const directFactoryCall =
              memberPath(node).length === 1 &&
              staticMemberName(node) === "extend" &&
              node.parent?.type === "CallExpression" &&
              node.parent.callee === node;
            if (!directFactoryCall) {
              context.report({ node, messageId: "factoryOnly" });
            }
          },
          CallExpression(node) {
            if (node.callee.type === "Identifier" && node.callee.name === "require") {
              context.report({ node, messageId: "factoryOnly" });
            }
            if (isPlaywrightFixtureFactoryCall(node) && !isExportedPlaywrightFixture(node.parent)) {
              context.report({ node, messageId: "factoryOnly" });
            }
            if (node.callee.type === "Identifier" && node.callee.name === "base") {
              context.report({ node, messageId: "factoryOnly" });
            }
          },
        };
      },
    },
  },
};

export default [
  {
    ignores: [
      "dist/**",
      "node_modules/**",
      "src/api/types.ts",
      "src/api/openapi.ts",
      "src/lib/types/openapi.ts",
    ],
  },
  {
    files: ["src/**/*.{ts,tsx}", allTestModules, "*.config.ts"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: 2022,
        sourceType: "module",
        ecmaFeatures: { jsx: true },
      },
      globals: {
        ...globals.browser,
        ...globals.es2022,
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
      react: reactPlugin,
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
      import: importPlugin,
      "jsx-a11y": jsxA11y,
    },
    rules: {
      ...jsxA11yRecommendedRules,
      // Base
      "no-console": ["warn", { allow: ["warn", "error"] }],
      "no-unused-vars": "off",
      // TypeScript
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/consistent-type-imports": "error",
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: [
                "@features/*/api/*",
                "@features/*/model/*",
                "@features/*/state/*",
                "@features/*/ui/*",
              ],
              message: "Import from the feature public index instead of deep feature layers.",
            },
          ],
        },
      ],
      // React
      "react/jsx-uses-react": "off",
      "react/react-in-jsx-scope": "off",
      "react/jsx-key": "error",
      "react-refresh/only-export-components": [
        "error",
        { allowConstantExport: true, allowExportNames: ["useSidebar"] },
      ],
      // React hooks
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      // Import
      "import/no-duplicates": "error",
      "import/no-restricted-paths": [
        "error",
        {
          zones: [
            { target: "./src/lib", from: "./src/shared" },
            { target: "./src/lib", from: "./src/features" },
            { target: "./src/lib", from: "./src/routes" },
            { target: "./src/lib", from: "./src/app" },
            { target: "./src/shared", from: "./src/features" },
            { target: "./src/shared", from: "./src/routes" },
            { target: "./src/shared", from: "./src/app" },
            { target: "./src", from: "./tests" },
          ],
        },
      ],
      "import/order": [
        "warn",
        {
          groups: ["builtin", "external", "internal", "parent", "sibling", "index"],
          "newlines-between": "always",
          alphabetize: { order: "asc" },
        },
      ],
    },
    settings: {
      react: { version: "detect" },
    },
  },
  {
    files: [allTestModules],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/consistent-type-imports": "off", // vi.importActual type parameter is not a type import
      "import/no-restricted-paths": "off",
      "no-restricted-imports": "off",
      "no-console": "off",
      "react-refresh/only-export-components": "off",
    },
  },
  {
    files: [allTestModules],
    ignores: ["tests/e2e/fixtures.ts"],
    plugins: {
      "tracefold-required-tests": requiredTestPolicyPlugin,
    },
    rules: {
      "tracefold-required-tests/fixed-declaration": "error",
    },
  },
  {
    files: ["tests/e2e/fixtures.ts"],
    plugins: {
      "tracefold-required-tests": requiredTestPolicyPlugin,
    },
    rules: {
      "tracefold-required-tests/playwright-fixture-factory": "error",
    },
  },
  {
    files: [
      `tests/architecture/**/*.${testModuleExtensions}`,
      `tests/unit/**/*.${testModuleExtensions}`,
      `tests/component/**/*.${testModuleExtensions}`,
      `tests/routes/**/*.${testModuleExtensions}`,
      `tests/e2e/full-stack/**/*.${testModuleExtensions}`,
    ],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "MemberExpression[property.name=/^(fail|fails|fixme|only|skip|todo)$/]",
          message:
            "Required tests must be plain passes; expected failures, disabled cases, and focused cases cannot authorize CI.",
        },
        {
          selector: "MemberExpression[property.value=/^(fail|fails|fixme|only|skip|todo)$/]",
          message:
            "Required tests must be plain passes; computed expected failures, disabled cases, and focused cases cannot authorize CI.",
        },
        {
          selector: "Property[key.name='fails']",
          message: "Required tests cannot mark an options object as an expected failure.",
        },
        {
          selector: "Property[key.value='fails']",
          message: "Required tests cannot hide an expected failure behind a computed options key.",
        },
        {
          selector:
            "CallExpression:has(Identifier[name=/^(it|test)$/]) > ObjectExpression.arguments > Property[key.name=/^(repeatEach|repeats|retries|retry)$/]",
          message: "Required tests cannot repeat or retry to green.",
        },
      ],
    },
  },
  {
    files: ["*.config.ts"],
    languageOptions: {
      globals: {
        ...globals.node,
      },
    },
  },
];
