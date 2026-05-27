# Homebrew formula for autosentry — deployed to ``ulmentflam/homebrew-tap``.
#
# Install:
#   brew install ulmentflam/tap/autosentry
#
# Upgrade:
#   brew upgrade autosentry
#
# On each autosentry release the ``brew-bump`` job in autosentry's
# ``.github/workflows/release.yml`` rewrites ``url`` + ``sha256`` below from
# the source tag tarball and pushes the change here. The Python dependency
# ``resource`` blocks are version-pinned and only need refreshing when
# autosentry's dependency closure changes — regenerate them with
# ``packaging/distribution/gen_resources.py`` (or ``brew
# update-python-resources`` on a non-nix Homebrew).
class Autosentry < Formula
  include Language::Python::Virtualenv

  desc "Self-healing supervisor for long-running processes"
  homepage "https://github.com/ulmentflam/autosentry"
  url "https://github.com/ulmentflam/autosentry/archive/refs/tags/v0.7.3.tar.gz"
  sha256 "05fc3ac9bc5787dcf0dadf499c88dc2472a90949baf09fa00bbf99eb9e9dc262"
  license "Apache-2.0"
  head "https://github.com/ulmentflam/autosentry.git", branch: "main"

  # Ordered build > normal to satisfy FormulaAudit/DependencyOrder.
  depends_on "rust" => :build # pydantic-core compiles from sdist via maturin/cargo
  depends_on "python@3.13"

  resource "annotated-doc" do
    url "https://files.pythonhosted.org/packages/57/ba/046ceea27344560984e26a590f90bc7f4a75b06701f653222458922b558c/annotated_doc-0.0.4.tar.gz"
    sha256 "fbcda96e87e9c92ad167c2e53839e57503ecfda18804ea28102353485033faa4"
  end

  resource "annotated-types" do
    url "https://files.pythonhosted.org/packages/ee/67/531ea369ba64dcff5ec9c3402f9f51bf748cec26dde048a2f973a4eea7f5/annotated_types-0.7.0.tar.gz"
    sha256 "aff07c09a53a08bc8cfccb9c85b05f1aa9a2a6f23728d790723543408344ce89"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/06/ff/7841249c247aa650a76b9ee4bbaeae59370dc8bfd2f6c01f3630c35eb134/markdown_it_py-4.2.0.tar.gz"
    sha256 "04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/d6/54/cfe61301667036ec958cb99bd3efefba235e65cdeb9c84d24a8293ba1d90/mdurl-0.1.2.tar.gz"
    sha256 "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/18/a5/b60d21ac674192f8ab0ba4e9fd860690f9b4a6e51ca5df118733b487d8d6/pydantic-2.13.4.tar.gz"
    sha256 "c40756b57adaa8b1efeeced5c196f3f3b7c435f90e84ea7f443901bec8099ef6"
  end

  resource "pydantic-core" do
    url "https://files.pythonhosted.org/packages/9d/56/921726b776ace8d8f5db44c4ef961006580d91dc52b803c489fafd1aa249/pydantic_core-2.46.4.tar.gz"
    sha256 "62f875393d7f270851f20523dd2e29f082bcc82292d66db2b64ea71f64b6e1c1"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/c3/b2/bc9c9196916376152d655522fdcebac55e66de6603a76a02bca1b6414f6c/pygments-2.20.0.tar.gz"
    sha256 "6757cd03768053ff99f3039c1a36d6c0aa0b263438fcab17520b30a303a82b5f"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/c0/8f/0722ca900cc807c13a6a0c696dacf35430f72e0ec571c4275d2371fca3e9/rich-15.0.0.tar.gz"
    sha256 "edd07a4824c6b40189fb7ac9bc4c52536e9780fbbfbddf6f1e2502c31b068c36"
  end

  resource "ruamel-yaml" do
    url "https://files.pythonhosted.org/packages/c7/3b/ebda527b56beb90cb7652cb1c7e4f91f48649fbcd8d2eb2fb6e77cd3329b/ruamel_yaml-0.19.1.tar.gz"
    sha256 "53eb66cd27849eff968ebf8f0bf61f46cdac2da1d1f3576dd4ccee9b25c31993"
  end

  resource "shellingham" do
    url "https://files.pythonhosted.org/packages/58/15/8b3609fd3830ef7b27b655beb4b4e9c62313a4e8da8c676e142cc210d58e/shellingham-1.5.4.tar.gz"
    sha256 "8dbca0739d487e5bd35ab3ca4b36e11c4078f3a234bfce294b0a0291363404de"
  end

  resource "tree-sitter" do
    url "https://files.pythonhosted.org/packages/66/7c/0350cfc47faadc0d3cf7d8237a4e34032b3014ddf4a12ded9933e1648b55/tree-sitter-0.25.2.tar.gz"
    sha256 "fe43c158555da46723b28b52e058ad444195afd1db3ca7720c59a254544e9c20"
  end

  resource "tree-sitter-language-pack" do
    url "https://files.pythonhosted.org/packages/2c/f3/fc0eabffb192ce49abee47e0bb3fea70fecfeacc1b8159e6f069795d2294/tree_sitter_language_pack-1.8.1.tar.gz"
    sha256 "800f8a92a2238e1b3abcc25cc86fd3a1b1d3392263c49266640c1b079a8e625e"
  end

  resource "typer" do
    url "https://files.pythonhosted.org/packages/67/a5/756f2e6bc81a7dd79aa3c625dd01b74cabc4516628cace2caaec09ca6ff2/typer-0.26.2.tar.gz"
    sha256 "9b4f19e08fcc9427a822d1ef467b1fe76737a2f65c7926bdeba2337d73569b68"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/72/94/1a15dd82efb362ac84269196e94cf00f187f7ed21c242792a923cdb1c61f/typing_extensions-4.15.0.tar.gz"
    sha256 "0cea48d173cc12fa28ecabc3b837ea3cf6f38c6d1136f85cbaaf598984861466"
  end

  resource "typing-inspection" do
    url "https://files.pythonhosted.org/packages/55/e3/70399cb7dd41c10ac53367ae42139cf4b1ca5f36bb3dc6c9d33acdb43655/typing_inspection-0.4.2.tar.gz"
    sha256 "ba561c48a67c5958007083d386c3295464928b01faa735ab8547c5692e87f464"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    # `autosentry --version` prints a banner that includes "v<version>".
    # Asserting the formula's own version appears catches a stale bottle
    # or a build that linked the wrong source tree.
    assert_match version.to_s, shell_output("#{bin}/autosentry --version")
  end
end
