{
  pkgs ? import <nixpkgs> { },
  lib ? pkgs.lib,
  mkShell ? pkgs.mkShell,
  callPackage ? pkgs.callPackage,
}:
let
  optionalAttrs = lib.attrsets.optionalAttrs;
  # define the nix-pyenv directory
  nix-pyenv-directory = ".nix-pyenv";
  # define version
  usingPython = pkgs.python3;
  # import required python packages
  requiredPythonPackages = callPackage ./py_requirements.nix { };
  # create python environment
  pyenv = usingPython.withPackages requiredPythonPackages;
  #
  callShellHookParam = {
    inherit
      nix-pyenv-directory
      pyenv
      usingPython
      pkgs
      ;
  };
  internalShell = mkShell ({
    # runtime deps live in .venv (managed by uv, see pyproject.toml);
    # bwrap + socat must both be present or the SDK sandbox silently fails open
    packages = [
      pyenv
      pkgs.uv
      pkgs.bubblewrap
      pkgs.socat
      pkgs.ripgrep
      # from nixpkgs, not pip: the wheel ships a generic-linux binary that
      # NixOS cannot exec (same reason cli_path gets pinned, see F9/F13)
      pkgs.ruff
    ];
  });
in
internalShell.overrideAttrs {
  shellHook = callPackage ./shellhook.nix (
    callShellHookParam
    // {
      inherit (internalShell) inputDerivation;
    }
  );
}
