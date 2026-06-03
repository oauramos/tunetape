# Homebrew formula for tunetape.
#
# Publishing checklist (each release):
#   1. Bump the version in pyproject.toml, commit, and push.
#   2. Cut a GitHub release/tag (e.g. v0.4.0). Then update `url` below and run:
#        curl -fsSL https://github.com/oauramos/tunetape/archive/refs/tags/v0.4.0.tar.gz | shasum -a 256
#      and paste the digest into `sha256`.
#   3. Refresh the resource blocks if `rich`'s dependency tree changed:
#        brew update-python-resources Formula/tunetape.rb
#   4. Copy this file into the tap repo (github.com/oauramos/homebrew-tunetape,
#      under Formula/) so `brew install oauramos/tunetape/tunetape` works.
#
# Local test before publishing:
#   brew install --build-from-source ./Formula/tunetape.rb
#   brew audit --strict --new Formula/tunetape.rb
#   brew test tunetape
class Tunetape < Formula
  include Language::Python::Virtualenv

  desc "Terminal audio player that streams from YouTube and KHInsider"
  homepage "https://github.com/oauramos/tunetape"
  url "https://github.com/oauramos/tunetape/archive/refs/tags/v0.5.0.tar.gz"
  sha256 "4107b90facdf14e58f0ddb174aa34147096fff0041307f9948f98a78da82b88c"
  license "MIT"

  depends_on "mpv"          # links FFmpeg libraries — provides audio decode + dynaudnorm
  depends_on "python@3.12"
  depends_on "yt-dlp"       # required for YouTube playback

  resource "rich" do
    url "https://files.pythonhosted.org/packages/e9/67/cae617f1351490c25a4b8ac3b8b63a4dda609295d8222bad12242dfdc629/rich-14.3.4.tar.gz"
    sha256 "817e02727f2b25b40ef56f5aa2217f400c8489f79ca8f46ea2b70dd5e14558a9"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/06/ff/7841249c247aa650a76b9ee4bbaeae59370dc8bfd2f6c01f3630c35eb134/markdown_it_py-4.2.0.tar.gz"
    sha256 "04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/d6/54/cfe61301667036ec958cb99bd3efefba235e65cdeb9c84d24a8293ba1d90/mdurl-0.1.2.tar.gz"
    sha256 "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/c3/b2/bc9c9196916376152d655522fdcebac55e66de6603a76a02bca1b6414f6c/pygments-2.20.0.tar.gz"
    sha256 "6757cd03768053ff99f3039c1a36d6c0aa0b263438fcab17520b30a303a82b5f"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "tunetape #{version}", shell_output("#{bin}/tunetape --version")
  end
end
