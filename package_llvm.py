#!/usr/bin/env python

import argparse
import os
import platform
import re
import requests
import stat
import shutil
import subprocess
import sys
import tarfile
import time

DIR_OF_THIS_SCRIPT = os.path.dirname( os.path.abspath( __file__ ) )

CHUNK_SIZE = 1024 * 1024 # 1MB

LLVM_RELEASE_URL = 'http://releases.llvm.org/{version}'
LLVM_PRERELEASE_URL = (
  'http://prereleases.llvm.org/{version}/rc{release_candidate}' )
LLVM_SOURCE = 'llvm-{version}.src'
CLANG_SOURCE = 'cfe-{version}.src'
BUNDLE_NAME = 'clang+llvm-{version}-{target}'
TARGET_REGEX = re.compile( '^Target: (?P<target>.*)$' )
BINTRAY_BASE_URL = 'https://api.bintray.com/'
BINTRAY_UPLOAD_URL = ( BINTRAY_BASE_URL +
                       'content/'
                       '{subject}/'
                       '{repo}/'
                       '{package}/'
                       '{version}/'
                       '{file_path}' )
BINTRAY_PUBLISH_URL = ( BINTRAY_BASE_URL +
                        'content/'
                        '{subject}/'
                        '{repo}/'
                        '{package}/'
                        '{version}/'
                        'publish' )
BINTRAY_DOWNLOAD_LIST_URL = ( BINTRAY_BASE_URL +
                              'file_metadata/'
                              '{subject}/'
                              '{repo}/'
                              '{file_path}' )
RETRY_INTERVAL = 10
SHARED_LIBRARY_REGEX = re.compile( '.*\.so(.\d+)*$' )


def Retries( function, *args ):
  max_retries = 3
  nb_retries = 0
  while True:
    try:
      function( *args )
    except SystemExit as error:
      nb_retries = nb_retries + 1
      print( 'ERROR: {0} Retry {1}. '.format( error, nb_retries ) )
      if nb_retries > max_retries:
        sys.exit( 'Number of retries exceeded ({0}). '
                  'Aborting.'.format( max_retries ) )
      time.sleep( RETRY_INTERVAL )
    else:
      return True


def Download( url ):
  dest = url.rsplit( '/', 1 )[ -1 ]
  print( 'Downloading {}.'.format( os.path.basename( dest ) ) )
  r = requests.get( url, stream = True )
  r.raise_for_status()
  with open( dest, 'wb') as f:
    for chunk in r.iter_content( chunk_size = CHUNK_SIZE ):
      if chunk:
        f.write( chunk )
  r.close()


def Extract( archive ):
  print( 'Extract archive {0}.'.format( archive ) )
  with tarfile.open( archive ) as f:
    f.extractall( '.' )


def GetLlvmBaseUrl( args ):
  if args.release_candidate:
    return LLVM_PRERELEASE_URL.format(
      version = args.version,
      release_candidate = args.release_candidate )

  return LLVM_RELEASE_URL.format( version = args.version )


def GetLlvmVersion( args ):
  if args.release_candidate:
    return args.version + 'rc' + str( args.release_candidate )
  return args.version


def DownloadLlvmSource( llvm_url, llvm_source ):
  llvm_archive = llvm_source + '.tar.xz'

  if not os.path.exists( llvm_archive ):
    Download( llvm_url + '/' + llvm_archive )

  if not os.path.exists( llvm_source ):
    Extract( llvm_archive )


def DownloadClangSource( llvm_url, clang_source ):
  clang_archive = clang_source + '.tar.xz'

  if not os.path.exists( clang_archive ):
    Download( llvm_url + '/' + clang_archive )

  if not os.path.exists( clang_source ):
    Extract( clang_archive )


def MoveClangSourceToLlvm( clang_source, llvm_source ):
  os.rename( clang_source, 'clang' )
  shutil.move(
    os.path.join( DIR_OF_THIS_SCRIPT, 'clang' ),
    os.path.join( DIR_OF_THIS_SCRIPT, llvm_source, 'tools' )
  )


def BuildLlvm( build_dir, install_dir, llvm_source ):
  try:
    os.chdir( build_dir )
    cmake = shutil.which( 'cmake' )
    # See https://llvm.org/docs/CMake.html#llvm-specific-variables for the CMake
    # variables defined by LLVM.
    subprocess.check_call( [
      cmake,
      '-G', 'Unix Makefiles',
      # A release build implies LLVM_ENABLE_ASSERTIONS=OFF.
      '-DCMAKE_BUILD_TYPE=Release',
      '-DCMAKE_INSTALL_PREFIX={}'.format( install_dir ),
      '-DLLVM_TARGETS_TO_BUILD=all',
      '-DLLVM_INCLUDE_EXAMPLES=OFF',
      '-DLLVM_INCLUDE_TESTS=OFF',
      '-DLLVM_INCLUDE_GO_TESTS=OFF',
      '-DLLVM_INCLUDE_DOCS=OFF',
      '-DLLVM_ENABLE_TERMINFO=OFF',
      '-DLLVM_ENABLE_ZLIB=OFF',
      '-DLLVM_ENABLE_LIBEDIT=OFF',
      '-DLLVM_ENABLE_LIBXML2=OFF',
      os.path.join( DIR_OF_THIS_SCRIPT, llvm_source )
    ] )

    subprocess.check_call( [ cmake, '--build', '.', '--target', 'install' ] )
  finally:
    os.chdir( DIR_OF_THIS_SCRIPT )


def GetTarget( install_dir ):
  output = subprocess.check_output(
    [ os.path.join( install_dir, 'bin', 'clang' ), '-###' ],
    stderr = subprocess.STDOUT ).decode( 'utf8' )
  for line in output.splitlines():
    match = TARGET_REGEX.search( line )
    if match:
      return match.group( 'target' )
  sys.exit( 'Cannot deduce LLVM target.' )


def BundleLlvm( install_dir, version ):
  target = GetTarget( install_dir )
  bundle_name = BUNDLE_NAME.format( version = version, target = target )
  archive_name = bundle_name + '.tar.xz'
  print( 'Bundling LLVM to {}.'.format( archive_name ) )
  with tarfile.open( name = archive_name, mode = 'w:xz' ) as tar_file:
    # The .so files are not set as executable.
    for root, directories, files in os.walk( install_dir ):
      for filename in files:
        filepath = os.path.join( root, filename )
        if SHARED_LIBRARY_REGEX.match( filename ):
          mode = os.stat( filepath ).st_mode
          # Add the executable bit only if the file is readable for the user.
          mode |= ( mode & 0o444 ) >> 2
          os.chmod( filepath, mode )
        arcname = os.path.join( bundle_name,
                                os.path.relpath( filepath, install_dir ) )
        tar_file.add( filepath, arcname = arcname )
  return os.path.join( DIR_OF_THIS_SCRIPT, archive_name )


def UploadLlvm( version, bundle_path, user_name, api_token ):
  bundle_name = os.path.basename( bundle_path )
  print( 'Uploading {} to Bintray.'.format( bundle_name ) )
  with open( bundle_path, 'rb' ) as bundle:
    response = requests.put(
      BINTRAY_UPLOAD_URL.format( subject = user_name,
                                 repo = 'llvm',
                                 package = 'llvm',
                                 version = version,
                                 file_path = bundle_name ),
      data = bundle,
      params = {
        'publish': 1,
        'override': 1
      },
      auth = ( user_name, api_token )
    )
    if response.status_code != 201:
      message = response.json()[ 'message' ]
      sys.exit( 'Upload failed with message: {}'.format( message ) )



def PublishLlvm( version, bundle_name, user_name, api_token ):
  print( 'Publishing {} on Bintray.'.format( bundle_name ) )
  response = requests.post(
    BINTRAY_PUBLISH_URL.format( subject = user_name,
                                repo = 'llvm',
                                package = 'llvm',
                                version = version ),
    json = {
      'discard': False,
      'publish_wait_for_secs': -1
    },
    auth = ( user_name, api_token )
  )
  if response.status_code != 200:
    message = response.json()[ 'message' ]
    sys.exit( 'Publish failed with message: {}'.format( message ) )


def ListLlvm( version, bundle_name, user_name, api_token):
  print( 'List {} on Bintray.'.format( bundle_name ) )
  response = requests.put(
    BINTRAY_DOWNLOAD_LIST_URL.format( subject = user_name,
                                      repo = 'llvm',
                                      file_path = bundle_name ),
    json = {
      'list_in_downloads': True
    },
    auth = ( user_name, api_token )
  )
  if response.status_code != 200:
    message = response.json()[ 'message' ]
    sys.exit( 'List failed with message: {}'.format( message ) )


def ParseArguments():
  parser = argparse.ArgumentParser()
  parser.add_argument( 'version', type = str, help = 'LLVM version.')
  parser.add_argument( '--release-candidate', type = int,
                       help = 'LLVM release candidate number.' )

  parser.add_argument( '--bt-user', action='store',
                       help = 'Bintray user name. Defaults to environment '
                              'variable: YCMD_BINTRAY_USERNAME' )
  parser.add_argument( '--bt-token', action='store',
                       help = 'Bintray api token. Defaults to environment '
                              'variable: YCMD_BINTRAY_API_TOKEN.' )

  args = parser.parse_args()

  if not args.bt_user:
    if 'YCMD_BINTRAY_USERNAME' not in os.environ:
      sys.exit( 'ERROR: Must specify either --bt-user or '
                'YCMD_BINTRAY_USERNAME in environment' )
    args.bt_user = os.environ[ 'YCMD_BINTRAY_USERNAME' ]

  if not args.bt_token:
    if 'YCMD_BINTRAY_API_TOKEN' not in os.environ:
      sys.exit( 'ERROR: Must specify either --bt-token or '
                'YCMD_BINTRAY_API_TOKEN in environment' )
    args.bt_token = os.environ[ 'YCMD_BINTRAY_API_TOKEN' ]

  return args


def Main():
  args = ParseArguments()
  llvm_url = GetLlvmBaseUrl( args )
  version = GetLlvmVersion( args )
  clang_source = CLANG_SOURCE.format( version = version )
  llvm_source = LLVM_SOURCE.format( version = version )
  if not os.path.exists( os.path.join( DIR_OF_THIS_SCRIPT, llvm_source ) ):
    DownloadLlvmSource( llvm_url, llvm_source )
  if not os.path.exists( os.path.join( DIR_OF_THIS_SCRIPT, llvm_source,
                                       'tools', 'clang' ) ):
    DownloadClangSource( llvm_url, clang_source )
    MoveClangSourceToLlvm( clang_source, llvm_source )
  build_dir = os.path.join( DIR_OF_THIS_SCRIPT, 'build' )
  install_dir = os.path.join( DIR_OF_THIS_SCRIPT, 'install' )
  if not os.path.exists( build_dir ):
    os.mkdir( build_dir )
  if not os.path.exists( install_dir ):
    os.mkdir( install_dir )
  BuildLlvm( build_dir, install_dir, llvm_source )
  bundle_path = BundleLlvm( install_dir, version )
  UploadLlvm( version, bundle_path, args.bt_user, args.bt_token )
  bundle_name = os.path.basename( bundle_path )
  PublishLlvm( version, bundle_name, args.bt_user, args.bt_token )
  Retries( ListLlvm, version, bundle_name, args.bt_user, args.bt_token )


if __name__ == "__main__":
  Main()
