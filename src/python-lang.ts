/*
 * Copyright (C) 2026 OpenMV, LLC.
 *
 * This software is licensed under terms that can be found in the
 * LICENSE file in the root directory of this software component.
 */
// Enhanced Python syntax highlighting for Monaco.
// Registers a Monarch tokenizer with support for builtins, decorators,
// self/cls, f-strings, function/class definitions, type hints, etc.

import * as monaco from "monaco-editor";

const keywords = [
  "False", "None", "True", "and", "as", "assert", "async", "await",
  "break", "class", "continue", "def", "del", "elif", "else", "except",
  "finally", "for", "from", "global", "if", "import", "in", "is",
  "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try",
  "while", "with", "yield",
];

const builtins = [
  "abs", "all", "any", "bin", "bool", "bytearray", "bytes", "callable",
  "chr", "classmethod", "compile", "complex", "delattr", "dict", "dir",
  "divmod", "enumerate", "eval", "exec", "filter", "float", "format",
  "frozenset", "getattr", "globals", "hasattr", "hash", "help", "hex",
  "id", "input", "int", "isinstance", "issubclass", "iter", "len",
  "list", "locals", "map", "max", "memoryview", "min", "next", "object",
  "oct", "open", "ord", "pow", "print", "property", "range", "repr",
  "reversed", "round", "set", "setattr", "slice", "sorted",
  "staticmethod", "str", "sum", "super", "tuple", "type", "vars", "zip",
  "BaseException", "Exception", "ArithmeticError", "AssertionError",
  "AttributeError", "BlockingIOError", "BrokenPipeError",
  "BufferError", "BytesWarning", "ChildProcessError",
  "ConnectionAbortedError", "ConnectionError", "ConnectionRefusedError",
  "ConnectionResetError", "DeprecationWarning", "EOFError",
  "EnvironmentError", "FileExistsError", "FileNotFoundError",
  "FloatingPointError", "FutureWarning", "GeneratorExit", "IOError",
  "ImportError", "ImportWarning", "IndentationError", "IndexError",
  "InterruptedError", "IsADirectoryError", "KeyError",
  "KeyboardInterrupt", "LookupError", "MemoryError", "ModuleNotFoundError",
  "NameError", "NotADirectoryError", "NotImplementedError",
  "OSError", "OverflowError", "PendingDeprecationWarning",
  "PermissionError", "ProcessLookupError", "RecursionError",
  "ReferenceError", "ResourceWarning", "RuntimeError",
  "RuntimeWarning", "StopAsyncIteration", "StopIteration",
  "SyntaxError", "SyntaxWarning", "SystemError", "SystemExit",
  "TabError", "TimeoutError", "TypeError", "UnboundLocalError",
  "UnicodeDecodeError", "UnicodeEncodeError", "UnicodeError",
  "UnicodeTranslationError", "UnicodeWarning", "UserWarning",
  "ValueError", "Warning", "ZeroDivisionError",
];

export function registerPythonLanguage() {
  monaco.languages.setMonarchTokensProvider("python", {
    keywords,
    builtins,

    brackets: [
      { open: "{", close: "}", token: "delimiter.curly" },
      { open: "[", close: "]", token: "delimiter.square" },
      { open: "(", close: ")", token: "delimiter.parenthesis" },
    ],

    tokenizer: {
      root: [
        // Decorators
        [/@\w+/, "tag"],

        // Function and class definitions
        [/\b(def)\b(\s+)(\w+)/, ["keyword", "white", "entity.name.function"]],
        [/\b(class)\b(\s+)(\w+)/, ["keyword", "white", "entity.name.class"]],

        // self / cls
        [/\b(self|cls)\b/, "variable.language"],

        // Keywords and identifiers
        [
          /[a-zA-Z_]\w*/,
          {
            cases: {
              "@keywords": "keyword",
              "@builtins": "support.function",
              "@default": "identifier",
            },
          },
        ],

        // Whitespace and comments
        { include: "@whitespace" },

        // Numbers
        [/0[xX][0-9a-fA-F](_?[0-9a-fA-F])*/, "number.hex"],
        [/0[oO][0-7](_?[0-7])*/, "number.octal"],
        [/0[bB][01](_?[01])*/, "number.binary"],
        [/\d[\d_]*(\.\d[\d_]*)?([eE][+-]?\d[\d_]*)?[jJ]?/, "number"],

        // Strings
        [/[fFbBuUrR]*"""/, "string", "@tdqs"],
        [/[fFbBuUrR]*'''/, "string", "@tsqs"],
        [/[fFbBuUrR]*"/, "string", "@dqs"],
        [/[fFbBuUrR]*'/, "string", "@sqs"],

        // Delimiters and operators
        [/->/, "operator"],
        [/[{}()\[\]]/, "@brackets"],
        [/[+\-*/%&|^~<>!=]=?/, "operator"],
        [/[;,.:@]/, "delimiter"],
      ],

      whitespace: [
        [/\s+/, "white"],
        [/#.*$/, "comment"],
      ],

      // Triple-double-quoted string
      tdqs: [
        [/[^"\\]+/, "string"],
        [/\\./, "string.escape"],
        [/"""/, "string", "@pop"],
        [/"/, "string"],
      ],

      // Triple-single-quoted string
      tsqs: [
        [/[^'\\]+/, "string"],
        [/\\./, "string.escape"],
        [/'''/, "string", "@pop"],
        [/'/, "string"],
      ],

      // Double-quoted string
      dqs: [
        [/[^"\\]+/, "string"],
        [/\\./, "string.escape"],
        [/"/, "string", "@pop"],
      ],

      // Single-quoted string
      sqs: [
        [/[^'\\]+/, "string"],
        [/\\./, "string.escape"],
        [/'/, "string", "@pop"],
      ],
    },
  });
}
