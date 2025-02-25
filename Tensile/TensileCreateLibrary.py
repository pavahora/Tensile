################################################################################
# Copyright 2016-2021 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell cop-
# ies of the Software, and to permit persons to whom the Software is furnished
# to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IM-
# PLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNE-
# CTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
################################################################################
# This script only gets called by CMake

if __name__ == "__main__":
    print("This file can no longer be run as a script.  Run 'Tensile/bin/TensileCreateLibrary' instead.")
    exit(1)

from . import Common
from . import EmbeddedData
from . import LibraryIO
from . import Utils
from .Common import globalParameters, HR, print1, print2, printExit, ensurePath, \
                    CHeader, CMakeHeader, assignGlobalParameters, \
                    listToInitializer, gfxName, architectureMap
from .KernelWriterAssembly import KernelWriterAssembly
from .KernelWriterSource import KernelWriterSource
from .SolutionLibrary import MasterSolutionLibrary
from .SolutionStructs import Solution
from .SolutionWriter import SolutionWriter

import argparse
import collections
import itertools
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from copy import deepcopy

################################################################################
def processKernelSource(kernel, kernelWriterSource, kernelWriterAssembly):
    """
    Generate source for a single kernel.
    Returns (error, source, header, kernelName).
    """
    try:
        kernelWriter = kernelWriterSource if kernel["KernelLanguage"] == "Source" else kernelWriterAssembly
        # get kernel name
        kernelName = kernelWriter.getKernelFileBase(kernel)
        (err, src) = kernelWriter.getSourceFileString(kernel)
        header = kernelWriter.getHeaderFileString(kernel)
    except RuntimeError:
        return (1, "", "", kernelName)

    return (err, src, header, kernelName)

def getAssemblyCodeObjectFiles(kernels, kernelWriterAssembly, outputPath):
    destDir = ensurePath(os.path.join(outputPath, 'library'))
    asmDir = kernelWriterAssembly.getAssemblyDirectory()
    assemblyKernels = list([k for k in kernels if k['KernelLanguage'] == 'Assembly'])
    if len(assemblyKernels) == 0:
        return []

    archs = collections.defaultdict(list)
    for k in assemblyKernels:
      archs[tuple(k['ISA'])].append(k)
    coFiles = []
    for arch, archKernels in archs.items():
      archName = gfxName(arch)
      objectFiles = list([kernelWriterAssembly.getKernelFileBase(k) + '.o' \
                          for k in archKernels \
                          if k['KernelLanguage'] == 'Assembly'])
      if len(objectFiles) == 0:
        continue
      if globalParameters["MergeFiles"] or globalParameters["NumMergedFiles"] > 1:
        coFile = os.path.join(destDir, 'TensileLibrary_{}.co'.format(archName))
        if "PackageLibrary" in globalParameters and globalParameters["PackageLibrary"]:
          destArchDir = ensurePath(os.path.join(destDir, archName))
          coFile = os.path.join(os.path.normcase(destArchDir), 'TensileLibrary_{}.co'.format(archName))

        if os.name == "nt":
          # On Windows, the objectFiles list command line (including spaces) 
          # exceeds the limit of 8191 characters, so using response file

          responseArgs = objectFiles
          responseFile = os.path.join(asmDir, 'clangArgs.txt')
          with open(responseFile, 'wt') as file:
            file.write( " ".join(responseArgs) )
            file.flush()

          args = [globalParameters['AssemblerPath'], '-target', 'amdgcn-amd-amdhsa', '-o', coFile, '@clangArgs.txt']
          subprocess.check_call(args, cwd=asmDir)
        else:
          args = kernelWriterAssembly.getLinkCodeObjectArgs(objectFiles, coFile)
          subprocess.check_call(args, cwd=asmDir)

        coFiles.append(coFile)
      else:
        # no mergefiles
        
        assemblyKernelNames = [kernelWriterAssembly.getKernelFileBase(k) for k in archKernels]
        origCOFiles = [os.path.join(asmDir,  k + '.co') for k in assemblyKernelNames]
        newCOFiles  = []
        if globalParameters["PackageLibrary"]:
          newCOFiles = [os.path.join(destDir, archName, k + '.co') for k in assemblyKernelNames]
        else:
          newCOFiles = [os.path.join(destDir, k + '_' + archName + '.co') for k in assemblyKernelNames]

        for src, dst in Utils.tqdm(zip(origCOFiles, newCOFiles), "Copying code objects"):
          shutil.copyfile(src, dst)
        coFiles += newCOFiles

    return coFiles

def which(p):
    exes = [p+x for x in ['.bat', '', '.exe']]  # bat may be front end for file with no extension
    system_path = os.environ['PATH'].split(os.pathsep)
    if p == 'hipcc' and 'CMAKE_CXX_COMPILER' in os.environ and os.path.isfile(os.environ['CMAKE_CXX_COMPILER']):
        return os.environ['CMAKE_CXX_COMPILER']
    for dirname in system_path+[globalParameters["ROCmBinPath"]]:
        for exe in exes:
            candidate = os.path.join(os.path.expanduser(dirname), exe)
            if os.path.isfile(candidate):
                return candidate
    return None

def splitArchs():
  # Helper for architecture
  def isSupported(arch):
    return globalParameters["AsmCaps"][arch]["SupportedISA"] and \
           globalParameters["AsmCaps"][arch]["SupportedSource"]

  if ";" in globalParameters["Architecture"]:
    wantedArchs = globalParameters["Architecture"].split(";")
  else:
    wantedArchs = globalParameters["Architecture"].split("_")
  archs = []
  cmdlineArchs = []
  if "all" in wantedArchs:
    for arch in globalParameters['SupportedISA']:
      if isSupported(arch):
        if (arch == (9,0,6) or arch == (9,0,8) or arch == (9,0,10)):
          if (arch == (9,0,10)):
            archs += [gfxName(arch) + '-xnack+']
            cmdlineArchs += [gfxName(arch) + ':xnack+']
          archs += [gfxName(arch) + '-xnack-']
          cmdlineArchs += [gfxName(arch) + ':xnack-']
        else:
          archs += [gfxName(arch)]
          cmdlineArchs += [gfxName(arch)]
  else:
    for arch in wantedArchs:
      archs += [re.sub(":", "-", arch)]
      cmdlineArchs += [arch]
  return archs, cmdlineArchs

def buildSourceCodeObjectFile(CxxCompiler, outputPath, kernelFile):
    buildPath = ensurePath(os.path.join(globalParameters['WorkingPath'], 'code_object_tmp'))
    destDir = ensurePath(os.path.join(outputPath, 'library'))
    (_, filename) = os.path.split(kernelFile)
    (base, _) = os.path.splitext(filename)

    if "CmakeCxxCompiler" in globalParameters and globalParameters["CmakeCxxCompiler"] is not None:
      os.environ["CMAKE_CXX_COMPILER"] = globalParameters["CmakeCxxCompiler"]

    objectFilename = base + '.o'
    soFilename = base + '.so'

    if (CxxCompiler == "hipcc"):
      archs, cmdlineArchs = splitArchs()

      archFlags = ['--offload-arch=' + arch for arch in cmdlineArchs]

      hipFlags = ["--genco", "-D__HIP_HCC_COMPAT_MODE__=1"] #needs to be fixed when Maneesh's change is made available

      hipFlags += ['-I', outputPath]

      launcher = shlex.split(os.environ.get('Tensile_CXX_COMPILER_LAUNCHER', ''))

      if os.name == "nt":
        hipFlags += ['-std=c++14', '-fms-extensions', '-fms-compatibility', '-fPIC', '-Wno-deprecated-declarations']
        compileArgs = launcher + [which('hipcc')] + hipFlags + archFlags + [kernelFile, '-c', '-o', os.path.join(buildPath, objectFilename)]
      else:
        compileArgs = launcher + [which('hipcc')] + hipFlags + archFlags + [kernelFile, '-c', '-o', os.path.join(buildPath, objectFilename)]

      if globalParameters["PrintCodeCommands"]:
        print('hipcc:', ' '.join(compileArgs))
      subprocess.check_call(compileArgs)

      infile = os.path.join(buildPath, objectFilename)
      try:
        bundlerArgs = [globalParameters["ClangOffloadBundlerPath"], "-type=o", "-inputs=%s" % infile, "-list"]
        listing = subprocess.check_output(bundlerArgs, stderr=subprocess.STDOUT).decode().split("\n")
        for target in listing:
          matched = re.search("gfx.*$", target)
          if matched:
            arch = re.sub(":", "-", matched.group())
            outfile = os.path.join(buildPath, "{0}-000-{1}.hsaco".format(soFilename, arch))
            bundlerArgs = [globalParameters["ClangOffloadBundlerPath"], "-type=o", "-targets=%s" % target, "-inputs=%s" % infile, "-outputs=%s" % outfile, "-unbundle"]
            if globalParameters["PrintCodeCommands"]:
              print(' '.join(bundlerArgs))
            subprocess.check_call(bundlerArgs)

      except subprocess.CalledProcessError:
        for i in range(len(archs)):
          outfile = os.path.join(buildPath, "{0}-000-{1}.hsaco".format(soFilename, archs[i]))
          bundlerArgs = [globalParameters["ClangOffloadBundlerPath"], "-type=o", "-targets=hip-amdgcn-amd-amdhsa--%s" % cmdlineArchs[i], "-inputs=%s" % infile, "-outputs=%s" % outfile, "-unbundle"]
          if globalParameters["PrintCodeCommands"]:
            print(' '.join(bundlerArgs))
          subprocess.check_call(bundlerArgs)

      coFilenames = ["{0}-000-{1}.hsaco".format(soFilename, arch) for arch in archs]
    else:
      raise RuntimeError("Unknown compiler {}".format(CxxCompiler))

    destCosList = []
    if "PackageLibrary" in globalParameters and globalParameters["PackageLibrary"]:
      for arch in archs:
        ensurePath(os.path.join(destDir, arch))
        archCoFilenames = [name for name in coFilenames if arch in name]
        extractedCOs = [os.path.join(buildPath, name) for name in archCoFilenames]
        destCOs = [os.path.join(destDir, arch, name) for name in archCoFilenames]
        destCosList += destCOs
        if globalParameters["PrintCodeCommands"]:
          print ("# copy source code objects    : ", extractedCOs)
          print ("# to dest source code objects : ", destCOs)
        for (src, dst) in zip(extractedCOs, destCOs):
          shutil.copyfile(src, dst)
    else:
      coFilenames = [name for name in coFilenames]
      extractedCOs = [os.path.join(buildPath, name) for name in coFilenames]
      destCOs = [os.path.join(destDir, name) for name in coFilenames]
      destCosList += destCOs
      for (src, dst) in zip(extractedCOs, destCOs):
        shutil.copyfile(src, dst)

    return destCosList

def buildSourceCodeObjectFiles(CxxCompiler, kernelFiles, outputPath):
    args = zip(itertools.repeat(CxxCompiler), itertools.repeat(outputPath), kernelFiles)

    coFiles = Common.ParallelMap(buildSourceCodeObjectFile, args, "Compiling source kernels",
                                 method=lambda x: x.starmap)

    return itertools.chain.from_iterable(coFiles)

################################################################################
def prepAsm(kernelWriterAssembly):
  """
  Create and prepare the assembly directory  - called ONCE per output dir:
  """
  asmPath = ensurePath(os.path.join(globalParameters["WorkingPath"], "assembly") )
  assemblerFileName = os.path.join(asmPath, \
      "asm-new.%s"%("bat" if os.name=="nt" else "sh"))
  assemblerFile = open(assemblerFileName, "w")
  if os.name == "nt":
    assemblerFile.write("echo Windows: Copying instead of Assembling\n")
    assemblerFile.write("copy %1.s %1.o\n")
    assemblerFile.write("copy %1.o %1.co\n")
  else:
    assemblerFile.write("#!/bin/sh {log}\n".format(log = "-x" if globalParameters["PrintLevel"] >=2  else ""))
    assemblerFile.write("# usage: asm-new.sh kernelName(no extension) [--wave32]\n")

    assemblerFile.write("f=$1\n")
    assemblerFile.write("shift\n")
    assemblerFile.write('if [ ! -z "$1" ] && [ "$1" = "--wave32" ]; then\n')
    assemblerFile.write("    wave=32\n")
    assemblerFile.write("    shift\n")
    assemblerFile.write("else\n")
    assemblerFile.write("    wave=64\n")
    assemblerFile.write("fi\n")


    isa = globalParameters["CurrentISA"]
    assemblerFile.write("h={gfxName}\n".format(gfxName = Common.gfxName(isa)))

    cArgs32 = kernelWriterAssembly.getCompileArgs("$f.s", "$f.o", isa=isa, wavefrontSize=32)
    cArgs64 = kernelWriterAssembly.getCompileArgs("$f.s", "$f.o", isa=isa, wavefrontSize=64)
    lArgs = kernelWriterAssembly.getLinkCodeObjectArgs(["$f.o"], "$f.co")

    assemblerFile.write("if [ $wave -eq 32 ]; then\n")
    assemblerFile.write(" ".join(cArgs32) + "\n")
    assemblerFile.write("else\n")
    assemblerFile.write(" ".join(cArgs64) + "\n")
    assemblerFile.write("fi\n")


    assemblerFile.write(" ".join(lArgs) + "\n")

    assemblerFile.write("cp $f.co ../../../library/${f}_$h.co\n")
    assemblerFile.write("mkdir -p ../../../asm_backup && ")
    assemblerFile.write("cp $f.s ../../../asm_backup/$f.s\n")

  assemblerFile.close()
  os.chmod(assemblerFileName, 0o777)

################################################################################
def buildKernelSourceAndHeaderFiles(results, outputPath, kernelsWithBuildErrs, \
      kernelSourceFile, kernelHeaderFile):
  """
  Logs errors and writes appropriate info to kernelSourceFile and kernelHeaderFile.

  Arguments:
    results:              list of (err, src, header, kernelName)
    outputPath:           path to source directory
    kernelsWithBuildErrs: Dictionary to be updated with kernels that have errors
    kernelSourceFile:     File to write source data to
    kernelHeaderFile:     File to write header data to

  Returns: 
    sourceFilenames:      Array containing source kernel filenames 
  """
  sourceFilenames = []

  # Find kernels to write
  kernelsToWrite = []
  for (err,src,header,kernelName) in results:

    # Keep track of kernels with errors
    if err:
      kernelsWithBuildErrs[kernelName] = err

    # Don't create a file for empty kernels
    if len(src.strip()) == 0:
      continue
    kernelsToWrite.append((err, src, header, kernelName))
  
  # Write kernel data to files
  if globalParameters["MergeFiles"]:

    # Merge all kernels into one file
    if globalParameters["NumMergedFiles"] == 1: 
      sourceFilenames.append(os.path.join(os.path.normcase(outputPath), "Kernels.cpp"))
      for (err,src,header,kernelName) in kernelsToWrite:
        kernelSourceFile.write(src)
        kernelHeaderFile.write(header)

    # Merge kernels into n seperate files
    else: 

      # Calculate num of kernels per file
      numValidKernels = len(kernelsToWrite)
      numMergedFiles = globalParameters["NumMergedFiles"]
      if numMergedFiles > numValidKernels:
        numMergedFiles = numValidKernels

      # Number of kernels per file (except the last file which gets this plus any remainder)
      kernelsPerFile = numValidKernels // numMergedFiles
    
      # Group kernel sources and headers into a list of lists for writing
      kernelSourceList = []
      kernelHeaderList = []
      for kernelIndex, (_,src,header,_) in enumerate(kernelsToWrite):
        makeNewFilegroup = kernelIndex % kernelsPerFile == 0
        isNotLastFile = len(kernelSourceList) < numMergedFiles
        if makeNewFilegroup and isNotLastFile:
          kernelHeaderList.append([])
          kernelSourceList.append([])
        kernelHeaderList[-1].append(src)
        kernelSourceList[-1].append(header)

      # Write source files
      for fileIndex, kernelSources in enumerate(kernelSourceList):
        kernelName = "Kernels" + str(fileIndex)
        kernelFilePath = os.path.join(outputPath, "Kernels", kernelName)

        with open(kernelFilePath + ".cpp", "w", encoding="utf-8") as kernelSourceFile:
          kernelSourceFile.write(CHeader)
          kernelSourceFile.write('#include "{}.h"\n'.format(kernelName))
          sourceFilenames.append(kernelFilePath + ".cpp")
          for src in kernelSources:
            kernelSourceFile.write(src)

      # Write header files
      for fileIndex, kernelHeaders in enumerate(kernelHeaderList):
        kernelName = "Kernels" + str(fileIndex)
        kernelFilePath = os.path.join(outputPath, "Kernels", kernelName)

        with open(kernelFilePath + ".h", "w", encoding="utf-8") as kernelHeaderFile:
          kernelHeaderFile.write(CHeader)
          kernelHeaderFile.write("#pragma once\n")
          if globalParameters["RuntimeLanguage"] == "HIP":
            kernelHeaderFile.write("#include <hip/hip_runtime.h>\n")
            kernelHeaderFile.write("#include <hip/hip_fp16.h>\n\n")
          kernelHeaderFile.write("#include <KernelHeader.h>\n\n")
          for header in kernelHeaders:
            kernelHeaderFile.write(header)

  # Write kernels into seperate files
  else: 
    for (err,src,header,kernelName) in kernelsToWrite:

      # write kernel.cpp
      filepath = os.path.join(outputPath, "Kernels", kernelName)
      sourceFilename = filepath + ".cpp"
      sourceFilenames.append(sourceFilename)
      kernelSourceFile = open(sourceFilename, "w", encoding="utf-8")
      kernelSourceFile.write(CHeader)
      kernelSourceFile.write(src)
      kernelSourceFile.close()

      # write kernel.h
      headerFilename = filepath + ".h"
      kernelHeaderFile = open(headerFilename, "w", encoding="utf-8")
      kernelHeaderFile.write(CHeader)
      kernelHeaderFile.write(header)
      kernelHeaderFile.close()
  
  return sourceFilenames

################################################################################
# Write Solutions and Kernels for BenchmarkClient or LibraryClient
################################################################################
def writeSolutionsAndKernels(outputPath, CxxCompiler, problemTypes, solutions, kernels, kernelHelperObjs, \
    solutionWriter, kernelWriterSource, kernelWriterAssembly, errorTolerant=False):
  start = time.time()

  codeObjectFiles = []

  # Push working path into build_tmp folder because there may be more than
  # one process running this script. This is to avoid build directory clashing.
  # NOTE: file paths must not contain the lower case word 'kernel' or the
  # /opt/rocm/bin/extractkernel will fail.
  # See buildSourceCodeObjectFile:167 for the call to this binary.
  Common.pushWorkingPath('build_tmp')
  Common.pushWorkingPath(os.path.basename(outputPath).upper())

  print1("# Writing Kernels...")
  kernelFiles = []
  kernelSourceFile = None
  kernelHeaderFile = None

  if not globalParameters["MergeFiles"] or globalParameters["NumMergedFiles"] > 1:
    ensurePath(os.path.join(outputPath, "Kernels"))

  ##############################################################################
  # Write Kernels
  ##############################################################################
  if globalParameters["MergeFiles"] and globalParameters["NumMergedFiles"] == 1:
    kernelSourceFilename = os.path.join(os.path.normcase(outputPath), "Kernels.cpp")
    kernelHeaderFilename = os.path.join(os.path.normcase(outputPath), "Kernels.h")

    kernelSourceFile = open(kernelSourceFilename, "w")
    kernelHeaderFile = open(kernelHeaderFilename, "w")
    kernelSourceFile.write(CHeader)
    kernelHeaderFile.write(CHeader)
    kernelSourceFile.write("#include \"Kernels.h\"\n")
    kernelHeaderFile.write("#pragma once\n")
    if globalParameters["RuntimeLanguage"] == "HIP":
      kernelHeaderFile.write("#include <hip/hip_runtime.h>\n")
      kernelHeaderFile.write("#include <hip/hip_ext.h>\n\n")
    kernelHeaderFile.write("#include \"KernelHeader.h\"\n\n")

  kernelsWithBuildErrs = {}

  prepAsm(kernelWriterAssembly)

  kIter = zip(kernels, itertools.repeat(kernelWriterSource), itertools.repeat(kernelWriterAssembly))
  results = Common.ParallelMap(processKernelSource, kIter, "Generating kernels", method=lambda x: x.starmap)

  removeKernels = []
  removeSolutions = []
  removeResults = []
  for kernIdx, res in Utils.tqdm(enumerate(results)):
    (err,src,header,kernelName) = res
    if(err == -2):
      removeKernels.append(kernels[kernIdx])
      removeSolutions.append(solutions[kernIdx])
      removeResults.append(results[kernIdx])
  for kern in removeKernels:
      kernels.remove(kern)
  for solut in removeSolutions:
      solutions.remove(solut)
  for rel in removeResults:
      results.remove(rel)

  kernelFiles += buildKernelSourceAndHeaderFiles(results, outputPath, kernelsWithBuildErrs, kernelSourceFile, kernelHeaderFile)

  kernelsToBuild = list(kernels)
  if errorTolerant:
      def success(kernel):
          writer = kernelWriterAssembly if kernel['KernelLanguage'] == 'Assembly' else kernelWriterSource
          kernelName = writer.getKernelName(kernel)
          return kernelName not in kernelsWithBuildErrs
      kernelsToBuild = list(filter(success, kernelsToBuild))

  if False:#len(kernelsWithBuildErrs) > 0:
    print("\nKernel compilation failed in one or more subprocesses. May want to set CpuThreads=0 and re-run to make debug easier")
    printExit("** kernel compilation failure **")

  # Put all kernel helper objects into the first merged kernel file
  if globalParameters["NumMergedFiles"] > 1 and len(kernelFiles) > 0:
    kernelFilename = kernelFiles[0].replace(".cpp", "")
    kernelSourceFile = open(kernelFilename + ".cpp", 'a', encoding="utf-8")
    kernelHeaderFile = open(kernelFilename + ".h", 'a', encoding="utf-8")

  # handle helper kernel function
  for ko in kernelHelperObjs:
    kernelName = ko.getKernelName()

    # write kernel.cpp
    if not globalParameters["MergeFiles"]:
      kernelSourceFilename = os.path.join(outputPath, "Kernels", kernelName+".cpp")
      kernelSourceFile = open(kernelSourceFilename, "w")
      kernelSourceFile.write(CHeader)
      kernelFiles.append(kernelSourceFilename)

    (err, src) = ko.getSourceFileString()
    kernelSourceFile.write(src)
    if err:
      print("*** warning: invalid kernel#%u"%kernelName)

    if not globalParameters["MergeFiles"]:
      kernelSourceFile.close()

    # write kernel.h
    if not globalParameters["MergeFiles"]:
      kernelHeaderFile = open(os.path.join(os.path.normcase(outputPath), "Kernels", kernelName + ".h"), "w")
      kernelHeaderFile.write(CHeader)

    kernelHeaderFile.write(ko.getHeaderFileString())

    if not globalParameters["MergeFiles"]:
      kernelHeaderFile.close()

  # close merged
  if globalParameters["MergeFiles"]:
    if kernelSourceFile:
      kernelSourceFile.close()
    if kernelHeaderFile:
      kernelHeaderFile.close()

  if not globalParameters["GenerateSourcesAndExit"]:
    codeObjectFiles += buildSourceCodeObjectFiles(CxxCompiler, kernelFiles, outputPath)
    codeObjectFiles += getAssemblyCodeObjectFiles(kernelsToBuild, kernelWriterAssembly, outputPath)

  stop = time.time()
  print("# Kernel Building elapsed time = %.1f secs" % (stop-start))

  Common.popWorkingPath() # build_tmp
  Common.popWorkingPath() # workingDir

  return codeObjectFiles

################################################################################
# Write Logic
################################################################################
def writeLogic(outputPath, logicData, solutionWriter ):
  print1("# Writing Library Logic")

  if not globalParameters["MergeFiles"] or globalParameters["NumMergedFiles"] > 1:
    ensurePath(os.path.join(outputPath, "Logic"))

  # Tensile.h
  h = ""
  h += "#pragma once\n"
  h += "#include \"TensileTypes.h\"\n"
  h += "#include \"SolutionHelper.h\"\n"
  h += "#include \"SolutionMapper.h\"\n"

  # TensileInternal.h
  ih = ""
  ih += "#include \"Tensile.h\"\n"

  # Tensile.cpp
  sourceIncludes = ""
  sourceIncludes += "#include \"Solutions.h\"\n"
  sourceIncludes += "#include \"Tensile.h\"\n"
  sourceIncludes += "#include \"TensileInternal.h\"\n"
  sourceIncludes += "#include \"SolutionMapper.h\"\n"

  s = sourceIncludes

  ########################################
  # problemType
  for problemType in Utils.tqdm(logicData):

    # function argument list
    argListSizes = solutionWriter.getArgList(problemType, False, False, False, False, True)
    argListData  = solutionWriter.getArgList(problemType, False,  True,  True,  True, True)
    argListAll   = solutionWriter.getArgList(problemType,  True,  True,  True,  True, True)

    # tensile initializer
    h += "\nvoid tensileInitialize();\n\n"

    # declare tensile_ProblemType
    h += "\n// enqueue solution\n"
    h += "TensileStatus tensile_%s(\n" % problemType
    for i in range(0, len(argListData)):
      h += "    %s %s = %s%s" \
          % (argListData[i][0], argListData[i][1], argListData[i][2], \
          ",\n" if i < len(argListData)-1 else ");\n\n")


    numSizes = problemType["TotalIndices"]
    assert(not problemType["UseInitialStridesCD"])
    firstStride = 0 if problemType["UseInitialStridesAB"] else 1
    lastStrideA = len(problemType["IndexAssignmentsA"])
    lastStrideB = len(problemType["IndexAssignmentsB"])
    lastStrideC = problemType["NumIndicesC"]
    lastStrideD = problemType["NumIndicesC"]
    h += "typedef ProblemKey<%u> ProblemKey_%s;\n" % (numSizes,problemType)
    h += "typedef ProblemDims<%u,%u,%u,%u,%u,%u> ProblemDims_%s;\n" \
        % (firstStride, lastStrideD, lastStrideC, lastStrideA, lastStrideB, numSizes, problemType)
    h += "typedef SolutionMapper<ProblemDims_%s, ProblemKey_%s> SolutionMapper_%s;\n" \
            % (problemType, problemType, problemType)

    # declare tensileGetSolutionPointer_ProblemType
    h += "\n// get solution pointer\n"
    h += "SolutionMapper_%s::SolutionRuntime *\n" % (problemType)
    h += "tensileGetSolutionPointer_%s(\n" % (problemType)
    for i in range(0, len(argListSizes)):
      h += "    %s %s%s" \
          % (argListSizes[i][0], argListSizes[i][1], \
          ",\n" if i < len(argListSizes)-1 else ");\n\n")

    # declare tensileName_
    h += "// get solution name\n"
    h += "const char * tensileGetSolutionName_%s(\n" \
        % (problemType)
    for i in range(0, len(argListSizes)):
      h += "    %s %s%s" \
          % (argListSizes[i][0], argListSizes[i][1], \
          ",\n" if i < len(argListSizes)-1 else ");\n\n")


    # get solution naming for problem type
    solutionsForProblemType = []
    for scheduleTuple in logicData[problemType]:
      solutionsForSchedule = scheduleTuple[2]
      for solution in solutionsForSchedule:
        if solution not in solutionsForProblemType:
          solutionsForProblemType.append(solution)

    # solution names for problem type
    solutionNamesForProblemType = []
    for solution in solutionsForProblemType:
      solutionName = solutionWriter.getSolutionName(solution)
      solutionNamesForProblemType.append(solutionName)

    # reset problemType source
    if not globalParameters["MergeFiles"] or globalParameters["NumMergedFiles"] > 1:
      filePrefix = "Tensile_%s" % (problemType)
      s = sourceIncludes

      for solutionName in solutionNamesForProblemType:
        s += "#include \"%s.h\"\n" % solutionName

    ########################################
    # Per-problem constants here:
    # These are common for all schedules and thus do not include schedule name (vega,hip,etc)
    s += "\n"
    s += "/*******************************************************************************\n"
    s += "* Per-Problem Functions for %s\n" % problemType
    s += "*******************************************************************************/\n"

    s += "// Problem type include the index assignments for free, summation, batch:\n"
    s += "static const ProblemType problemType_%s( " % problemType
    s += listToInitializer(problemType["IndicesFree"]) + ", "
    s += listToInitializer(problemType["IndicesSummation"]) + ", "
    s += listToInitializer(problemType["IndicesBatch"]) + ", "
    s += listToInitializer(problemType["IndexAssignmentsA"]) + ", "
    s += listToInitializer(problemType["IndexAssignmentsB"])
    s += ");\n"

    s += "\n"
    s += "// Master solution mapper is the entry point for problem->solution mapping\n"
    s += "// There is one master solution mapper per problem type\n"
    s += "// The master solution mapper contains pointers to the solution mappers for each device\n"
    s += "static MasterSolutionMapper<ProblemDims_%s> masterSolutionMapper_%s;\n " % (problemType,problemType)


    ########################################
    # implement per-Schedule functions in source
    s += "\n"
    s += "/*******************************************************************************\n * Per-Schedule Functions\n *******************************************************************************/"
    for scheduleTuple in logicData[problemType]:

      # get logic parameters for problem type
      scheduleName  = scheduleTuple[0]
      deviceNames   = scheduleTuple[1]
      solutionsForSchedule = scheduleTuple[2]
      indexOrder    = scheduleTuple[3]
      exactLogic    = scheduleTuple[4]
      rangeLogic    = scheduleTuple[5]

      # solution names for schedule
      solutionNamesForSchedule = []
      for solution in solutionsForSchedule:
        solutionName = solutionWriter.getSolutionName(solution)
        solutionNamesForSchedule.append(solutionName)

      s += "\n\n"
      schedProbName = "%s_%s" % (scheduleName, problemType)
      s += writeSolutionAndExactTable(scheduleName, deviceNames, schedProbName, problemType, \
              solutionsForSchedule, solutionNamesForSchedule, exactLogic)


    # Per-problem function here:
    # function tensileGetSolutionPointer_ProblemType
    del schedProbName
    del scheduleName
    s += "\n// problem dims -> solution logic\n"
    s += "SolutionMapper_%s::SolutionRuntime *\n" % (problemType)
    s += "tensileGetSolutionPointer_%s(\n" % (problemType)
    for i in range(0, len(argListSizes)):
      s += "    %s %s%s" \
          % (argListSizes[i][0], argListSizes[i][1], \
          ",\n" if i < len(argListSizes)-1 else ") {\n\n")

    exactLogicStr = writeExactLogic(problemType, indexOrder, \
                                    solutionsForSchedule, exactLogic, \
                                    solutionNamesForSchedule, True)
    if rangeLogic != None:
      print("** warning: ignored ranges in logic file, these should have been expanded with ExpandRanges=1 during Tensile phase 3")
    s += "  /* exact mappings */\n"
    s += exactLogicStr
    s += "\n  return nullptr;\n"
    s += "\n}\n"

    # function tensileGetSolutionName_Schedule_ProblemType
    s += "\n// get solution name for problem dims\n"
    s += "const char * tensileGetSolutionName_%s(\n" \
        % (problemType)
    for i in range(0, len(argListSizes)):
      s += "    %s %s%s" \
          % (argListSizes[i][0], argListSizes[i][1], \
          ",\n" if i < len(argListSizes)-1 else ") {\n\n")

    exactLogicStr = writeExactLogic(problemType, indexOrder, \
                                    solutionsForSchedule, exactLogic, \
                                    solutionNamesForSchedule, False)
    s += "  /* exact mappings */\n"
    s += exactLogicStr
    s += "\n}\n"

    ########################################
    # implement problem-type functions in source
    s += "/*******************************************************************************\n * Per-ProblemType Functions\n *******************************************************************************/"


    # declare tensile_ProblemType
    s += "\n// main call to solution; enqueues a kernel\n"
    s += "TensileStatus tensile_%s(\n" % problemType
    for i in range(0, len(argListData)):
      s += "    %s %s%s" \
          % (argListData[i][0], argListData[i][1], \
          ",\n" if i < len(argListData)-1 else ") {\n")
    s += "    auto solution = tensileGetSolutionPointer_%s(\n" % (problemType)
    for i in range(0, len(argListSizes)):
      s += "        %s%s" \
          % (argListSizes[i][1], ", " if i < len(argListSizes)-1 else ");")
      s += "\n"
    s += "    if (solution) {\n"
    s += "      TensileSolutionPointer_%s f = reinterpret_cast<TensileSolutionPointer_%s> (solution->_info->_functionPtr);\n" \
      % (problemType, problemType)
    s += "      auto solutionLock = &solution->_lock;\n"
    s += "      return f("
    for i in range(0, len(argListAll)):
      s += "%s%s" \
          % (argListAll[i][1], ", " if i < len(argListAll)-1 else ");\n")
    s += "    } else {\n"
    #s += "      printf(\"solution not valid, returning fail\\n\");"
    s += "      return tensileStatusFailure; // no solution found\n"
    s += "    }\n"
    s += "}\n"

    # open and close problemType files
    if not globalParameters["MergeFiles"] or globalParameters["NumMergedFiles"] > 1:
      logicSourceFile = open(os.path.join(os.path.normcase(outputPath), "Logic", \
          "%s.cpp" % filePrefix), "w")
      logicSourceFile.write(s)
      logicSourceFile.close()

  s += "\n"
  s += writeTensileInitialize(logicData)

  # close merged files
  if globalParameters["MergeFiles"] and globalParameters["NumMergedFiles"] == 1:
    logicSourceFile = open(os.path.join(os.path.normcase(outputPath), \
        "Tensile.cpp"), "w")
    logicSourceFile.write(s)
    logicSourceFile.close()

  logicHeaderFile = open(os.path.join(os.path.normcase(outputPath), \
      "Tensile.h"), "w")
  logicHeaderFile.write(h)
  logicHeaderFile.close()

  internalHeaderFile = open(os.path.join(os.path.normcase(outputPath), \
      "TensileInternal.h"), "w")
  internalHeaderFile.write(ih)
  internalHeaderFile.close()


def writeTensileInitialize(logicData):

  s = "/*******************************************************************************\n"
  s += "* Tensilze initializer\n"
  s += "*******************************************************************************/\n"
  s += "void tensileInitialize() {\n"

  for problemType in logicData:
    s += "  masterSolutionMapper_%s.initialize();\n" % problemType

    for scheduleTuple in logicData[problemType]:
      scheduleName  = scheduleTuple[0]
      deviceNames   = scheduleTuple[1]


      schedProbName = "%s_%s" % (scheduleName, problemType)
      s += "  solutionMapper_%s.initializeMappers(" % (schedProbName)
      s += "{%s}," % (', '.join('"{0}"'.format(w) for w in deviceNames))
      s += "&masterSolutionMapper_%s);\n" % (problemType)

  s += "}"

  return s

def writeSolutionAndExactTable(scheduleName, deviceNames, schedProbName, problemType, \
                               solutionsForSchedule, solutionNames, exactLogic):
  s = ""
  s += "namespace { // Start schedule '%s'\n" % scheduleName

  s += "// solution table - function, name, assertion requirements\n"
  s += "static const SolutionInfo solutionTable_%s[] = {\n" % (schedProbName)
  for i in range(0, len(solutionsForSchedule)):
    solution = solutionsForSchedule[i]
    solutionName = solutionNames[i]
    s += "  {(void*)%s, \"%s\", {%d, %d, %d, %d, %d, %d, %d} }%s // %d" % \
      (solutionName, solutionName, \
        solution["AssertSummationElementMultiple"], \
        solution["AssertFree0ElementMultiple"], \
        solution["AssertFree1ElementMultiple"], \
        solution["AssertMinApproxSize"], \
        False, \
        solution["PackBatchDims"]==2, \
        solution["PackBatchDims"]==1, \
        "," if i < len(solutionsForSchedule)-1 else "", \
        i)
    s += "\n"

  s += "};\n\n"

  # Write the exact problems here
  s += "// table of exact problem dims and selected solutionIdx\n"
  s += "static const std::pair<const ProblemKey_%s, int> embeddedExactTable_%s[] = {\n" % (problemType,schedProbName)
  numSizes = problemType["TotalIndices"]
  for ruleIdx in range(0, len(exactLogic)):
    rule = exactLogic[ruleIdx]
    problemSize = rule[0][:numSizes]
    solutionIdx = rule[1][0]
    solutionGFlops = rule[1][1]
    s += " { {"
    for i in range(0, len(problemSize)):
      if i == 0:
        s += "%u" % problemSize[i]
      else:
        s += ", %u" % problemSize[i]
    s += "}, %u}" % (solutionIdx)
    s += "," if ruleIdx != len(exactLogic)-1 else " "
    s += " // %.0f GFlop/s" % (solutionGFlops)
    s += "\n"
  s += "};\n\n"

  # Create a solution mapper and init with the table above:
  s += "// The solution master constructor here adds device to the master solution mapper\n"
  s += "// The entrypoint to find a solution for this problem is through the master solution master\n"
  s += "static SolutionMapper_%s solutionMapper_%s(\n" % (problemType, schedProbName)
  s += "  \"%s\", // schedule+problem name\n" % (schedProbName)
  s += "  solutionTable_%s, %u,\n" % (schedProbName, len(solutionsForSchedule))
  s += "  embeddedExactTable_%s, %u,\n" % (schedProbName, len(exactLogic))
  s += "  &problemType_%s);\n" % (problemType)

  s += "} // end anonymous namespace\n"
  return s


################################################################################
# Write Range Logic Recursive
# ptr :
#   True : write logic to return the function pointer
#   False : write logic to return the function name
################################################################################
def writeExactLogic(problemType, indexOrder,
                    solutionsForSchedule, exactLogic, \
                    solutionNames, ptr):
  s = ""
  s += "  ProblemDims_%s pdims(" % problemType
  indexChars = globalParameters["IndexChars"]
  firstStrideAB = 0 if problemType["UseInitialStridesAB"] else 1
  firstStrideCD = 0 if problemType["UseInitialStridesCD"] else 1
  lastStrideD = problemType["NumIndicesC"]
  lastStrideC = problemType["NumIndicesC"]
  lastStrideA = len(problemType["IndexAssignmentsA"])
  lastStrideB = len(problemType["IndexAssignmentsB"])
  for i in range(firstStrideCD,lastStrideD):
    if i != firstStrideCD: s += ", "
    s += "strideD%u%s" % (i, indexChars[i])
  for i in range(firstStrideCD,lastStrideC):
    s += ", strideC%u%s" % (i, indexChars[i])
  for i in range(firstStrideAB,lastStrideA):
    s += ", strideA%u%s" % (i, \
        indexChars[problemType["IndexAssignmentsA"][i]])
  for i in range(firstStrideAB,lastStrideB):
    s += ", strideB%u%s" % (i, \
        indexChars[problemType["IndexAssignmentsB"][i]])
  for i in range(0,len(indexOrder)):
    s += ", size%s" % indexChars[i]
  s += ");\n"

  s += "  auto solutionMapper = reinterpret_cast<SolutionMapper_%s *> (masterSolutionMapper_%s.mapper());\n"  \
      % (problemType, problemType)
  if ptr:
    s += "  return solutionMapper->getSolutionWithFallback(pdims,&masterSolutionMapper_%s);\n" % problemType
  else:
    s += "  return solutionMapper->getSolutionWithFallback(pdims,&masterSolutionMapper_%s)->_info->_name;\n" % problemType

  return s


################################################################################
# Write Solution Call
################################################################################
def writeSolutionCall(solutionName, problemType):
  indexChars = globalParameters["IndexChars"]
  s = ""
  s += "%s(" % solutionName
  # solution parameters
  s += " dataD, dataC, dataA, dataB, alpha"
  if problemType["UseBeta"]:
    s += ", beta"
  s += ", offsetC, offsetA, offsetB"
  firstStrideAB = firstStrideCD = 1
  if problemType["UseInitialStridesAB"]:
    firstStrideAB = 0
  if problemType["UseInitialStridesCD"]:
    firstStrideCD = 0
  lastStrideD = problemType["NumIndicesC"]
  lastStrideC = problemType["NumIndicesC"]
  lastStrideA = len(problemType["IndexAssignmentsA"])
  lastStrideB = len(problemType["IndexAssignmentsB"])
  for i in range(firstStrideCD,lastStrideD):
    s += ", strideD%u%s" % (i, indexChars[i])
  for i in range(firstStrideCD,lastStrideC):
    s += ", strideC%u%s" % (i, indexChars[i])
  for i in range(firstStrideAB,lastStrideA):
    s += ", strideA%u%s" % (i, \
        indexChars[problemType["IndexAssignmentsA"][i]])
  for i in range(firstStrideAB,lastStrideB):
    s += ", strideB%u%s" % (i, \
        indexChars[problemType["IndexAssignmentsB"][i]])
  for i in range(0, problemType["TotalIndices"]):
    s += ", size%s" % indexChars[i]
  s += ", stream, numInputEvents, inputEvents, outputEvent )"
  return s

##############################################################################
# Min Naming / Solution and Kernel Writers
##############################################################################
def getSolutionAndKernelWriters(solutions, kernels):

  # if any kernels are assembly, append every ISA supported
  solutionSerialNaming = Solution.getSerialNaming(solutions)
  kernelSerialNaming   = Solution.getSerialNaming(kernels)

  solutionMinNaming    = Solution.getMinNaming(solutions)
  kernelMinNaming      = Solution.getMinNaming(kernels)
  solutionWriter       = SolutionWriter(solutionMinNaming, solutionSerialNaming, kernelMinNaming, kernelSerialNaming)
  kernelWriterSource   = KernelWriterSource(kernelMinNaming, kernelSerialNaming)
  kernelWriterAssembly = KernelWriterAssembly(kernelMinNaming, kernelSerialNaming)

  return (solutionWriter, kernelWriterSource, kernelWriterAssembly, kernelMinNaming, solutionMinNaming)

################################################################################
# copy static cpp files and headers
################################################################################
def copyStaticFiles(outputPath):
  libraryStaticFiles = [
    "TensileTypes.h",
    "tensile_bfloat16.h",
    "KernelHeader.h" ]

  for fileName in libraryStaticFiles:
    # copy file
    shutil.copy( os.path.join(globalParameters["SourcePath"], fileName), \
        outputPath )

  return libraryStaticFiles

def buildObjectFileNames(solutionWriter, kernelWriterSource, kernelWriterAssembly, solutions, kernels, kernelHelperObjs):

  # Build lists of output object names
  sourceKernelNames = []
  asmKernelNames = []
  kernelHelperObjNames = []

  solutionFiles = []
  sourceKernelFiles = []
  asmKernelFiles = []
  sourceLibFiles = []
  asmLibFiles = []

  sourceKernels = list([k for k in kernels if k['KernelLanguage'] == 'Source'])
  asmKernels = list([k for k in kernels if k['KernelLanguage'] == 'Assembly'])

  # Build a list of kernel object names.
  for kernel in sourceKernels:
    sourceKernelNames += [kernelWriterSource.getKernelFileBase(kernel)]

  for kernel in asmKernels:
    asmKernelNames += [kernelWriterAssembly.getKernelFileBase(kernel)]

  kernelHelperObjNames = [ko.getKernelName() for ko in kernelHelperObjs]

  cxxCompiler = globalParameters["CxxCompiler"]

  # Source based kernels are built for all supported architectures
  if (cxxCompiler == 'hipcc'):
    sourceArchs, _ = splitArchs()
  else:
    raise RuntimeError("Unknown compiler %s" % cxxCompiler)

  # Asm based kernels target the configured ISA
  asmArchs = collections.defaultdict(list)
  for (kernelName, kernel) in zip(asmKernelNames, asmKernels):
    asmArchs[kernelName].append(gfxName(kernel['ISA']))

  # Build a list of source files
  if not globalParameters["MergeFiles"]:
    for kernelName in (sourceKernelNames + asmKernelNames + kernelHelperObjNames):
      sourceKernelFiles += [
        "%s.h"   % (kernelName),
        "%s.cpp" % (kernelName)]
  elif globalParameters["NumMergedFiles"] > 1:
    for kernelIndex in range(0, globalParameters["NumMergedFiles"]):
      sourceKernelFiles += [
        "Kernels%s.h"   % str(kernelIndex),
        "Kernels%s.cpp" % str(kernelIndex)]
    for kernelName in (kernelHelperObjNames):
      sourceKernelFiles += [
        "%s.h"   % (kernelName),
        "%s.cpp" % (kernelName)]
  else:
    sourceKernelFiles += ["Kernels.h", "Kernels.cpp"]

  # Build a list of assembly files
  for asmKernelName in asmKernelNames:
      asmKernelFiles += [
        "%s.s"  % (asmKernelName),
        "%s.o"  % (asmKernelName),
        "%s.co" % (asmKernelName)]

  # Build a list of lib names from source
  if not globalParameters["MergeFiles"]:

    allSources = sourceKernelNames + kernelHelperObjNames

    for kernelName in (allSources):
      if (cxxCompiler == 'hipcc'):
        sourceLibFiles += ["%s.so-000-%s.hsaco" % (kernelName, arch) for arch in sourceArchs]
      else:
        raise RuntimeError("Unknown compiler {}".format(cxxCompiler))
  elif globalParameters["NumMergedFiles"] > 1:
    if (cxxCompiler == 'hipcc'):
      for kernelIndex in range(0, globalParameters["NumMergedFiles"]):
        sourceLibFiles += ["Kernels%d.so-000-%s.hsaco" % (kernelIndex, arch) for arch in sourceArchs]
    else:
      raise RuntimeError("Unknown compiler {}".format(cxxCompiler))
  else: # Merge
    if (cxxCompiler == 'hipcc'):
      sourceLibFiles += ["Kernels.so-000-%s.hsaco" % (arch) for arch in sourceArchs]
    else:
      raise RuntimeError("Unknown compiler {}".format(cxxCompiler))

  # Build a list of asm lib names
  if globalParameters["MergeFiles"]:
    # Find all unique arch values for current asm kernels
    uniqueArchs = set(itertools.chain(*asmArchs.values()))
    asmLibFiles += ["TensileLibrary_%s.co" % (arch) for arch in uniqueArchs]
  else:
    for asmKernelName, archs in asmArchs.items():
      asmLibFiles += ["%s_%s.co" % (asmKernelName, str(arch)) for arch in archs]

  return (solutionFiles, sourceKernelFiles, asmKernelFiles, sourceLibFiles, asmLibFiles)

def buildObjectFilePaths(prefixDir, solutionFiles, sourceKernelFiles, asmKernelFiles, sourceLibFiles, asmLibFiles):

  solutionPaths = []
  sourceKernelPaths = []
  asmKernelPaths = []
  sourceLibPaths = []
  asmLibPaths = []

  # Build full paths for source kernel files
  sourceKernelDir = ""
  if not globalParameters["MergeFiles"] or globalParameters["NumMergedFiles"] > 1:
    sourceKernelDir = os.path.join(prefixDir, "Kernels")
  else:
    sourceKernelDir = prefixDir

  for sourceKernelFile in sourceKernelFiles:
    sourceKernelPaths += [ os.path.join(sourceKernelDir, sourceKernelFile) ]

  # Build full paths for asm kernel files
  asmKernelDir = os.path.join(prefixDir, "assembly")

  for asmKernelFile in asmKernelFiles:
    asmKernelPaths += [ os.path.join(asmKernelDir, asmKernelFile) ]

  # Build full paths for source and asm library files
  libDir = os.path.join(prefixDir, "library")

  for sourceLibFile in sourceLibFiles:
    sourceLibPaths += [ os.path.join(libDir, sourceLibFile) ]

  for asmLibFile in asmLibFiles:
    if globalParameters["PackageLibrary"]:
      # Asm lib files are enumerated in the form of
      # KernelName_gfxXXXXX.co
      # Strip the gfxXXXX portion and use that as a subdirectory
      asmLibFileNoExt = str(os.path.splitext(asmLibFile)[0])
      asmArch = asmLibFileNoExt[asmLibFileNoExt.find("_gfx"):]

      # asmArch contains _gfxXXXX. Don't use the underscore in new path
      asmLibPaths += [ os.path.join(
        libDir, asmArch[1:], asmLibFile.replace(asmArch, ''))]

    else:
      asmLibPaths += [ os.path.join(libDir, asmLibFile) ]

  return (solutionPaths, sourceKernelPaths, asmKernelPaths, sourceLibPaths, asmLibPaths)

################################################################################
# Write CMake
################################################################################
def writeCMake(outputPath, solutionFiles, kernelFiles, libraryStaticFiles):
  print1("# Writing Custom CMake")

  # Build output file paths, using relative CMake symbol
  cmakeSrcDir = "${CMAKE_SOURCE_DIR}"
  (solutionPaths, sourceKernelPaths, asmKernelPaths, sourceLibPaths, asmLibPaths) = \
    buildObjectFilePaths(cmakeSrcDir, solutionFiles, kernelFiles, [], [], [])

  # Build full paths the static library files
  staticFilePaths = []
  for staticFile in libraryStaticFiles:
    staticFilePaths += [ os.path.join(cmakeSrcDir, staticFile) ]

  # Proceed to generate cmake file
  generatedFile = open(os.path.join(os.path.normcase(outputPath), "Generated.cmake"), "w")
  generatedFile.write(CMakeHeader)

  # write TensileClient_KERNELS symbol
  generatedFile.write("set( TensileClient_KERNELS\n")
  for kernelFile in sourceKernelPaths:
    generatedFile.write("  %s\n" % (kernelFile))
  generatedFile.write("  )\n")

  # write TensileClient_SOURCE symbol
  generatedFile.write("set( TensileClient_SOURCE\n")
  for fileName in libraryStaticFiles:
    generatedFile.write("  ${CMAKE_SOURCE_DIR}/%s\n" % fileName)
  generatedFile.write("  )\n\n")

  generatedFile.close()

################################################################################
# Generate Kernel Objects From Solutions
################################################################################
def generateKernelObjectsFromSolutions(solutions):
  # create solution writer and kernel writer
  kernels = []
  kernelHelperObjs = []
  kernelHelperNames = set()

  for solution in solutions:
    solutionKernels = solution.getKernels()
    for kernel in solutionKernels:
      if kernel not in kernels:
        kernels.append(kernel)
    solutionHelperKernels = solution.getHelperKernelObjects()
    for ko in solutionHelperKernels:
      kname = ko.getKernelName()
      if kname not in kernelHelperNames:
        kernelHelperObjs.append(ko)
        kernelHelperNames.add(kname)

  return (kernels, kernelHelperObjs, kernelHelperNames)

################################################################################
# Write Benchmark Client Files
################################################################################
def writeBenchmarkClientFiles(libraryWorkingPath, tensileSourcePath, solutions, cxxCompiler):

  if not globalParameters["GenerateSourcesAndExit"]:
      copyStaticFiles(libraryWorkingPath)

  kernels, kernelsBetaOnly, _ = generateKernelObjectsFromSolutions(solutions)
  solutionWriter, kernelWriterSource, kernelWriterAssembly, \
    kernelMinNaming, _ = getSolutionAndKernelWriters(solutions, kernels)

  # write solution, kernels and CMake
  problemType = solutions[0]["ProblemType"]
  codeObjectFiles = writeSolutionsAndKernels( \
    libraryWorkingPath, cxxCompiler, [problemType], solutions, kernels, kernelsBetaOnly, \
    solutionWriter, kernelWriterSource, kernelWriterAssembly, errorTolerant=True )

  newLibraryDir = ensurePath(os.path.join(libraryWorkingPath, 'library'))
  newLibraryFile = os.path.join(newLibraryDir, "TensileLibrary.yaml")
  newLibrary = MasterSolutionLibrary.BenchmarkingLibrary(solutions)
  newLibrary.applyNaming(kernelMinNaming)

  LibraryIO.writeYAML(newLibraryFile, Utils.state(newLibrary))

  return (codeObjectFiles, newLibrary)

def WriteClientLibraryFromSolutions(solutionList, libraryWorkingPath, tensileSourcePath = None):

  if tensileSourcePath == None:
    tensileSourcePath = os.path.dirname(os.path.realpath(__file__))
  firstSolution = deepcopy(solutionList[0])
  problemType = firstSolution["ProblemType"].state
  problemType["DataType"] = problemType["DataType"].value
  problemType["DestDataType"] = problemType["DestDataType"].value
  problemType["ComputeDataType"] = problemType["ComputeDataType"].value
  cxxCompiler = globalParameters["CxxCompiler"]

  effectiveWorkingPath = os.path.join(libraryWorkingPath, "library")
  ensurePath(effectiveWorkingPath)
  mataDataFilePath = os.path.join(effectiveWorkingPath, 'metadata.yaml')

  metaData = {"ProblemType":problemType}
  LibraryIO.writeYAML(mataDataFilePath, metaData)

  codeObjectFiles, newLibrary = writeBenchmarkClientFiles(libraryWorkingPath, tensileSourcePath, solutionList, cxxCompiler )

  return (codeObjectFiles, newLibrary)

################################################################################
# Tensile Create Library
################################################################################
def TensileCreateLibrary():
  print1("")
  print1(HR)
  print1("# Tensile Create Library")
  print2(HR)
  print2("")

  ##############################################################################
  # Parse Command Line Arguments
  ##############################################################################
  def splitExtraParameters(par):
    """
    Allows the --global-parameters option to specify any parameters from the command line.
    """

    (key, value) = par.split("=")
    value = eval(value)
    return (key, value)

  print2("Arguments: %s" % sys.argv)
  argParser = argparse.ArgumentParser()
  argParser.add_argument("LogicPath",       help="Path to LibraryLogic.yaml files.")
  argParser.add_argument("OutputPath",      help="Where to write library files?")
  argParser.add_argument("RuntimeLanguage", help="Which runtime language?", choices=["OCL", "HIP", "HSA"])
  argParser.add_argument("--cxx-compiler",           dest="CxxCompiler",       choices=["hipcc"],       action="store", default="hipcc")
  argParser.add_argument("--cmake-cxx-compiler",     dest="CmakeCxxCompiler",  action="store")
  argParser.add_argument("--code-object-version",    dest="CodeObjectVersion", choices=["V2", "V3"], action="store", default="V3")
  argParser.add_argument("--architecture",           dest="Architecture",      type=str, action="store", default="all", help="Supported archs: " + " ".join(architectureMap.keys()))
  argParser.add_argument("--merge-files",            dest="MergeFiles",        action="store_true")
  argParser.add_argument("--no-merge-files",         dest="MergeFiles",        action="store_false")
  argParser.add_argument("--num-merged-files",       dest="NumMergedFiles",    type=int, default=1, help="Number of files the kernels should be written into.")
  argParser.add_argument("--short-file-names",       dest="ShortNames",        action="store_true")
  argParser.add_argument("--no-short-file-names",    dest="ShortNames",        action="store_false")
  argParser.add_argument("--library-print-debug",    dest="LibraryPrintDebug", action="store_true")
  argParser.add_argument("--no-library-print-debug", dest="LibraryPrintDebug", action="store_false")
  argParser.add_argument("--no-enumerate",           action="store_true", help="Do not run rocm_agent_enumerator.")
  argParser.add_argument("--package-library",        dest="PackageLibrary",    action="store_true", default=False)
  argParser.add_argument("--embed-library",          dest="EmbedLibrary",
                         help="Embed (new) library files into static variables.  Specify the name of the library.")

  argParser.add_argument("--embed-library-key",      dest="EmbedLibraryKey", default=None,
                         help="Access key for embedding library files.")
  argParser.add_argument("--version", help="Version string to embed into library file.")
  argParser.add_argument("--generate-manifest-and-exit",   dest="GenerateManifestAndExit", action="store_true",
                          default=False, help="Output manifest file with list of expected library objects and exit.")
  argParser.add_argument("--library-format", dest="LibraryFormat", choices=["yaml", "msgpack"], \
      action="store", default="msgpack", help="select which library format to use")
  argParser.add_argument("--generate-sources-and-exit",   dest="GenerateSourcesAndExit", action="store_true",
                          default=False, help="Output source files only and exit.")
  argParser.add_argument("--jobs", "-j", dest="CpuThreads", type=int,
                          default=-1, help="Number of parallel jobs to launch.")
  argParser.add_argument("--verbose", "-v", dest="PrintLevel", type=int,
                          default=1, help="Set printout verbosity level.")

  argParser.add_argument("--global-parameters", nargs="+", type=splitExtraParameters, default=[])
  args = argParser.parse_args()

  logicPath = args.LogicPath
  outputPath = args.OutputPath
  CxxCompiler = args.CxxCompiler
  libraryFormat = args.LibraryFormat
  print2("OutputPath: %s" % outputPath)
  ensurePath(outputPath)
  outputPath = os.path.abspath(outputPath)
  arguments = {}
  arguments["RuntimeLanguage"] = args.RuntimeLanguage
  arguments["CodeObjectVersion"] = args.CodeObjectVersion
  arguments["Architecture"] = args.Architecture
  arguments["CxxCompiler"] = args.CxxCompiler
  if args.CmakeCxxCompiler:
    os.environ["CMAKE_CXX_COMPILER"] = args.CmakeCxxCompiler
  arguments["MergeFiles"] = args.MergeFiles
  arguments["NumMergedFiles"] = args.NumMergedFiles
  arguments["ShortNames"] = args.ShortNames
  arguments["LibraryPrintDebug"] = args.LibraryPrintDebug
  arguments["CodeFromFiles"] = False
  arguments["EmbedLibrary"] = args.EmbedLibrary
  arguments["LibraryFormat"] = args.LibraryFormat
  if args.no_enumerate:
    arguments["ROCmAgentEnumeratorPath"] = False
  arguments["PackageLibrary"] = args.PackageLibrary

  arguments["GenerateManifestAndExit"] = args.GenerateManifestAndExit

  arguments["GenerateSourcesAndExit"] = args.GenerateSourcesAndExit
  if arguments["GenerateSourcesAndExit"]:
    # Generated sources are preserved and go into output dir
    arguments["WorkingPath"] = outputPath

  arguments["CpuThreads"] = args.CpuThreads
  arguments["PrintLevel"] = args.PrintLevel

  for key, value in args.global_parameters:
    arguments[key] = value

  assignGlobalParameters(arguments)

  print1("# CodeObjectVersion from TensileCreateLibrary: %s" % arguments["CodeObjectVersion"])
  print1("# CxxCompiler       from TensileCreateLibrary: %s" % CxxCompiler)
  print1("# Architecture      from TensileCreateLibrary: %s" % arguments["Architecture"])
  print1("# LibraryFormat     from TensileCreateLibrary: %s" % libraryFormat)

  if not os.path.exists(logicPath):
    printExit("LogicPath %s doesn't exist" % logicPath)

  if ";" in arguments["Architecture"]:
    archs = arguments["Architecture"].split(";") # user arg list format
  else:
    archs = arguments["Architecture"].split("_") # workaround for cmake list in list issue
  logicArchs = set()
  for arch in archs:
    if arch in architectureMap:
      logicArchs.add(architectureMap[arch])
    else:
      printExit("Architecture %s not supported" % arch)

  # Recursive directory search
  logicFiles = []
  for root, dirs, files in os.walk(logicPath):
    logicFiles += [os.path.join(root, f) for f in files
                       if os.path.splitext(f)[1]==".yaml" \
                       and (any(logicArch in os.path.splitext(f)[0] for logicArch in logicArchs) \
                       or "hip" in os.path.splitext(f)[0]) ]

  print1("# LibraryLogicFiles:" % logicFiles)
  for logicFile in logicFiles:
    print1("#   %s" % logicFile)

  ##############################################################################
  # Parse config files
  ##############################################################################
  solutions = []
  logicData = {} # keys are problemTypes, values are schedules

  libraries = Common.ParallelMap(LibraryIO.parseLibraryLogicFile, logicFiles, "Reading logic files")

  masterLibraries = {}
  fullMasterLibrary = None
  for logic in Utils.tqdm(libraries, "Processing logic data"):
    (scheduleName, deviceNames, problemType, solutionsForSchedule, \
       indexOrder, exactLogic, rangeLogic, newLibrary, architectureName) = logic

    if globalParameters["PackageLibrary"]:
      if architectureName in masterLibraries:
        masterLibraries[architectureName].merge(deepcopy(newLibrary))
      else:
        masterLibraries[architectureName] = deepcopy(newLibrary)
        masterLibraries[architectureName].version = args.version
    else:
      if fullMasterLibrary is None:
        fullMasterLibrary = deepcopy(newLibrary)
        fullMasterLibrary.version = args.version
      else:
        fullMasterLibrary.merge(deepcopy(newLibrary))

    if problemType not in logicData:
      logicData[problemType] = []
    logicData[problemType].append((scheduleName, deviceNames, \
        solutionsForSchedule, indexOrder, exactLogic, rangeLogic ))

    for solution in solutionsForSchedule:
      if solution not in solutions:
        solutions.append(solution)

  kernels, kernelHelperObjs, _ = generateKernelObjectsFromSolutions(solutions)

  # if any kernels are assembly, append every ISA supported
  solutionWriter, kernelWriterSource, kernelWriterAssembly, \
    kernelMinNaming, _ = getSolutionAndKernelWriters(solutions, kernels)

  staticFiles = copyStaticFiles(outputPath)

  # Build a list of files to be expected
  (solutionFiles,
   sourceKernelFiles,
   asmKernelFiles,
   sourceLibFiles,
   asmLibFiles) = buildObjectFileNames(solutionWriter, kernelWriterSource, \
    kernelWriterAssembly, solutions, kernels, kernelHelperObjs)

  (_,
   _,
   _,
   sourceLibPaths,
   asmLibPaths) = buildObjectFilePaths(outputPath, solutionFiles, sourceKernelFiles, \
    asmKernelFiles, sourceLibFiles, asmLibFiles)

  # Generate manifest file
  libraryPath = os.path.join(outputPath, "library")
  ensurePath(libraryPath)
  generatedFile = open(os.path.join(libraryPath, "TensileManifest.txt"), "w")

  libraryFilename = "TensileLibrary.yaml" if globalParameters["LibraryFormat"] == "yaml" else "TensileLibrary.dat"

  # Manifest file contains YAML file, output library paths and cpp source for embedding.
  for filePath in [os.path.join(libraryPath, libraryFilename)] + sourceLibPaths + asmLibPaths:
    generatedFile.write("%s\n" %(filePath) )
  generatedFile.close()

  if globalParameters["GenerateManifestAndExit"] == True:
    return

  # generate cmake for the source kernels
  if not arguments["GenerateSourcesAndExit"]:
    writeCMake(outputPath, solutionFiles, sourceKernelFiles, staticFiles)

  # Make sure to copy the library static files.
  for fileName in staticFiles:
    shutil.copy( os.path.join(globalParameters["SourcePath"], fileName), \
      outputPath )

  # write solutions and kernels
  problemTypes = list(logicData.keys())

  codeObjectFiles = writeSolutionsAndKernels(outputPath, CxxCompiler, problemTypes, solutions,
                                             kernels, kernelHelperObjs, solutionWriter, kernelWriterSource, kernelWriterAssembly)
  
  bothLibSet = set(sourceLibPaths + asmLibPaths)
  setA = set( map( os.path.normcase, set(codeObjectFiles) ) ) 
  setB = set( map( os.path.normcase, bothLibSet ) )

  sanityCheck0 = setA - setB 
  sanityCheck1 = setB - setA 

  if globalParameters["PrintCodeCommands"]:
    print("codeObjectFiles:", codeObjectFiles)
    print("sourceLibPaths + asmLibPaths:", sourceLibPaths + asmLibPaths)

  assert len(sanityCheck0) == 0, "Unexpected code object files: {}".format(sanityCheck0)
  if not globalParameters["GenerateSourcesAndExit"]:
    assert len(sanityCheck1) == 0, "Missing expected code object files: {}".format(sanityCheck1)

  archs = [gfxName(arch) for arch in globalParameters['SupportedISA'] \
             if globalParameters["AsmCaps"][arch]["SupportedISA"]]
  newLibraryDir = ensurePath(os.path.join(outputPath, 'library'))

  if globalParameters["PackageLibrary"]:
    for archName, newMasterLibrary in masterLibraries.items():
      if (archName in archs):
        archPath = ensurePath(os.path.join(newLibraryDir, archName))
        masterFile = os.path.join(archPath, "TensileLibrary")
        newMasterLibrary.applyNaming(kernelMinNaming)
        LibraryIO.write(masterFile, Utils.state(newMasterLibrary), args.LibraryFormat)
  else:
    masterFile = os.path.join(newLibraryDir, "TensileLibrary")
    fullMasterLibrary.applyNaming(kernelMinNaming)
    LibraryIO.write(masterFile, Utils.state(fullMasterLibrary), args.LibraryFormat)

  theMasterLibrary = fullMasterLibrary
  if globalParameters["PackageLibrary"]:
    theMasterLibrary = list(masterLibraries.values())[0]

  if args.EmbedLibrary is not None:
      embedFileName = os.path.join(outputPath, "library/{}.cpp".format(args.EmbedLibrary))
      with EmbeddedData.EmbeddedDataFile(embedFileName) as embedFile:

          ext = ".yaml" if globalParameters["LibraryFormat"] == "yaml" else ".dat"
          embedFile.embed_file(theMasterLibrary.cpp_base_class, masterFile + ext, nullTerminated=True,
                               key=args.EmbedLibraryKey)

          for co in Utils.tqdm(codeObjectFiles):
              embedFile.embed_file("SolutionAdapter", co, nullTerminated=False,
                                   key=args.EmbedLibraryKey)

  print1("# Tensile Library Writer DONE")
  print1(HR)
  print1("")
  