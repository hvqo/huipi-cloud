import js from '@eslint/js'
import ts from 'typescript-eslint'
import vue from 'eslint-plugin-vue'
export default ts.config(
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      'src/api/generated.ts',
      'playwright-report/**',
      'test-results/**',
      '.cache/**',
    ],
  },
  js.configs.recommended,
  ...ts.configs.recommended,
  ...vue.configs['flat/essential'],
  { files: ['**/*.vue'], languageOptions: { parserOptions: { parser: ts.parser } } },
  {
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      'no-undef': 'off',
      'vue/multi-word-component-names': 'off',
    },
  },
)
